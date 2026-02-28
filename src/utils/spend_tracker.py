"""Token usage and cost tracker for LLM calls made during an audit run.

Usage
-----
Import TRACKER in any module that invokes an LLM, then pass it as a callback:

    from src.utils.spend_tracker import TRACKER
    llm.invoke(messages, config={"callbacks": [TRACKER]})

At the end of a run, call TRACKER.summary() to get a dict of totals, or
TRACKER.report() for a human-readable string.  Call TRACKER.reset() between
runs if the process is long-lived.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

# ---------------------------------------------------------------------------
# Pricing table (USD per token) — Anthropic public pricing as of 2026-02.
#
# Keys are substrings matched against the model name returned by the API
# (case-insensitive, first match wins).  Add new model generations here.
#
# Anthropic does NOT return cost in API responses; token counts are returned
# and we calculate cost here.  For automatic cost tracking without maintaining
# this table, consider enabling LangSmith (set LANGCHAIN_API_KEY).
# ---------------------------------------------------------------------------
_PRICING: dict[str, dict[str, float]] = {
    # claude-haiku-4 / claude-haiku-4-5  (new naming scheme, 2025+)
    "haiku-4":      {"input": 0.80 / 1_000_000, "output": 4.00 / 1_000_000},
    # claude-3-5-haiku / claude-3-haiku  (legacy naming)
    "haiku":        {"input": 0.80 / 1_000_000, "output": 4.00 / 1_000_000},
    # claude-sonnet-4 / claude-3-5-sonnet / claude-3-sonnet
    "sonnet":       {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
    # claude-opus-4 / claude-3-opus
    "opus":         {"input": 15.00 / 1_000_000, "output": 75.00 / 1_000_000},
    # fallback — used when the model name doesn't match any key above
    "default":      {"input": 0.80 / 1_000_000, "output": 4.00 / 1_000_000},
}


def _price_per_token(model: str) -> dict[str, float]:
    m = model.lower()
    for key, rates in _PRICING.items():
        if key != "default" and key in m:
            return rates
    return _PRICING["default"]


# ---------------------------------------------------------------------------
# Per-call record
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    model: str
    node: str
    input_tokens: int
    output_tokens: int

    @property
    def input_cost(self) -> float:
        return self.input_tokens * _price_per_token(self.model)["input"]

    @property
    def output_cost(self) -> float:
        return self.output_tokens * _price_per_token(self.model)["output"]

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.output_cost

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "node": self.node,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": round(self.input_cost, 6),
            "output_cost_usd": round(self.output_cost, 6),
            "total_cost_usd": round(self.total_cost, 6),
        }


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------


class SpendTracker(BaseCallbackHandler):
    """LangChain callback that records token usage and estimated cost per LLM call.

    Uses threading.local() for the active-node label so parallel LangGraph nodes
    (repo_investigator and doc_analyst fan-out) each tag their own LLM calls
    correctly without overwriting each other.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[CallRecord] = []
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Thread-local active-node property
    # ------------------------------------------------------------------

    @property
    def _active_node(self) -> str:
        return getattr(self._local, "node", "unknown")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_node(self, node_name: str) -> None:
        """Call before each detective dimension runs to tag its LLM calls."""
        self._local.node = node_name

    def reset(self) -> None:
        """Clear all records — call between runs in long-lived processes."""
        self.records.clear()
        self._local.node = "unknown"

    def summary(self) -> dict:
        """Return aggregated totals and per-call breakdown as a dict."""
        total_input = sum(r.input_tokens for r in self.records)
        total_output = sum(r.output_tokens for r in self.records)
        total_cost = sum(r.total_cost for r in self.records)
        by_node: dict[str, dict] = {}
        for r in self.records:
            node = by_node.setdefault(r.node, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0})
            node["input_tokens"] += r.input_tokens
            node["output_tokens"] += r.output_tokens
            node["cost_usd"] += r.total_cost
            node["calls"] += 1
        for node in by_node.values():
            node["cost_usd"] = round(node["cost_usd"], 6)
        return {
            "total_calls": len(self.records),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "total_cost_usd": round(total_cost, 6),
            "by_node": by_node,
            "calls": [r.as_dict() for r in self.records],
        }

    def report(self) -> str:
        """Return a human-readable spend report string."""
        s = self.summary()
        lines = [
            "── Spend Summary ────────────────────────────────────────────────",
            f"  Total LLM calls : {s['total_calls']}",
            f"  Input tokens    : {s['total_input_tokens']:,}",
            f"  Output tokens   : {s['total_output_tokens']:,}",
            f"  Estimated cost  : ${s['total_cost_usd']:.4f} USD",
            "",
            "  By node:",
        ]
        for node, data in s["by_node"].items():
            lines.append(
                f"    {node:<30} calls={data['calls']}  "
                f"in={data['input_tokens']:,}  out={data['output_tokens']:,}  "
                f"~${data['cost_usd']:.4f}"
            )
        lines.append("─────────────────────────────────────────────────────────────────")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # LangChain callback hook
    # ------------------------------------------------------------------

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Capture token usage from every LLM response.

        Prefers AIMessage.usage_metadata (standardized across all LangChain
        providers, added in langchain-core 0.1) over the raw response_metadata
        dict, which is provider-specific.  Falls back gracefully if neither
        is populated.
        """
        model = ""
        input_tokens = 0
        output_tokens = 0

        for generations in response.generations:
            for gen in generations:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue

                # ── Primary: standardized usage_metadata (langchain-core ≥ 0.1) ──
                usage_meta = getattr(msg, "usage_metadata", None)
                if usage_meta:
                    input_tokens += usage_meta.get("input_tokens", 0)
                    output_tokens += usage_meta.get("output_tokens", 0)
                else:
                    # ── Fallback: provider-specific response_metadata ────────────
                    meta: dict = getattr(msg, "response_metadata", {}) or {}
                    usage: dict = meta.get("usage", {})
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)

                # Model name: response_metadata is the reliable source for this
                if not model:
                    meta = getattr(msg, "response_metadata", {}) or {}
                    model = meta.get("model", "") or ""

        if input_tokens or output_tokens:
            self.records.append(
                CallRecord(
                    model=model or "unknown",
                    node=self._active_node,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            )


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly
# ---------------------------------------------------------------------------

TRACKER = SpendTracker()
