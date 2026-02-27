"""Step-by-step activity logger for detective LLM agent loops.

Hooks into LangChain callbacks to print a real-time trace of:
  - Which node / dimensions are being investigated
  - Every LLM call (turn number, model)
  - Every tool call (name + args)
  - Every tool result (name + truncated output)
  - Evidence emitted per dimension

Usage
-----
Import LOGGER and pass it as a callback alongside TRACKER:

    from src.utils.audit_logger import LOGGER
    config = {"callbacks": [TRACKER, LOGGER]}
    llm.invoke(messages, config=config)
    tool_fn.invoke(args, config=config)

Call LOGGER.set_node(name, dimensions) at the start of each detective node.
"""

from __future__ import annotations

import json
import textwrap
import threading
from typing import Any, Union

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

# Max characters to show for tool args / results before truncating
_ARG_LIMIT = 300
_RESULT_LIMIT = 400


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"  … [{len(text) - limit} chars truncated]"


def _fmt_json(obj: Any, limit: int) -> str:
    try:
        raw = json.dumps(obj, default=str)
    except Exception:
        raw = str(obj)
    return _truncate(raw, limit)


class AuditLogger(BaseCallbackHandler):
    """Real-time console logger for detective agent activity.

    Uses threading.local() for per-thread node/turn counters so parallel
    LangGraph nodes (repo_investigator and doc_analyst fan-out) log under their
    own labels without races.
    """

    def __init__(self, *, verbose: bool = True) -> None:
        super().__init__()
        self.verbose = verbose
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Thread-local helpers
    # ------------------------------------------------------------------

    @property
    def _node(self) -> str:
        return getattr(self._local, "node", "unknown")

    @property
    def _dimensions(self) -> list[str]:
        return getattr(self._local, "dimensions", [])

    @property
    def _llm_turn(self) -> int:
        return getattr(self._local, "llm_turn", 0)

    @_llm_turn.setter
    def _llm_turn(self, value: int) -> None:
        self._local.llm_turn = value

    @property
    def _tool_turn(self) -> int:
        return getattr(self._local, "tool_turn", 0)

    @_tool_turn.setter
    def _tool_turn(self, value: int) -> None:
        self._local.tool_turn = value

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def set_node(self, node_name: str, dimensions: list[dict]) -> None:
        """Call at the start of each detective dimension to reset context."""
        self._local.node = node_name
        self._local.dimensions = [d["id"] for d in dimensions]
        self._local.llm_turn = 0
        self._local.tool_turn = 0
        if self.verbose:
            dim_list = ", ".join(self._local.dimensions)
            print(f"\n{'='*70}")
            print(f"  NODE: {node_name}")
            print(f"  Dimensions ({len(self._local.dimensions)}): {dim_list}")
            print(f"{'='*70}")

    def log_evidence(self, evidences: list) -> None:
        """Call after evidence emission to show the final verdict per dimension."""
        if not self.verbose:
            return
        print(f"\n  [{self._node}] Evidence emitted:")
        for ev in evidences:
            icon = "✓" if ev.found else "✗"
            print(f"    {icon} {ev.goal:<40} confidence={ev.confidence:.2f}")
            if ev.rationale:
                wrapped = textwrap.fill(
                    ev.rationale[:200],
                    width=70,
                    initial_indent="        ",
                    subsequent_indent="        ",
                )
                print(wrapped)

    # ------------------------------------------------------------------
    # LangChain callback hooks
    # ------------------------------------------------------------------

    def on_chat_model_start(
        self,
        serialized: dict,
        messages: list,
        **kwargs: Any,
    ) -> None:
        if not self.verbose:
            return
        self._llm_turn += 1
        model = (serialized.get("kwargs") or {}).get("model", "llm")
        print(f"\n  [{self._node}] LLM turn #{self._llm_turn}  model={model}")

    def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        **kwargs: Any,
    ) -> None:
        if not self.verbose:
            return
        self._tool_turn += 1
        tool_name = serialized.get("name", "unknown_tool")
        args_display = _truncate(input_str, _ARG_LIMIT)
        print(f"  [{self._node}]   → tool call #{self._tool_turn}: {tool_name}")
        print(f"             args: {args_display}")

    def on_tool_end(
        self,
        output: Union[str, Any],
        **kwargs: Any,
    ) -> None:
        if not self.verbose:
            return
        result_display = _truncate(str(output), _RESULT_LIMIT)
        print(f"  [{self._node}]   ← result: {result_display}")

    def on_tool_error(
        self,
        error: BaseException,
        **kwargs: Any,
    ) -> None:
        if not self.verbose:
            return
        print(f"  [{self._node}]   ✗ tool error: {error}")

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        if not self.verbose:
            return
        # Check if the response contains tool calls or a final text answer
        has_tool_calls = False
        for generations in response.generations:
            for gen in generations:
                msg = getattr(gen, "message", None)
                if msg and getattr(msg, "tool_calls", None):
                    has_tool_calls = True
                    for tc in msg.tool_calls:
                        args_display = _fmt_json(tc.get("args", {}), _ARG_LIMIT)
                        print(
                            f"  [{self._node}]   queued tool: {tc['name']}  "
                            f"args={args_display}"
                        )
        if not has_tool_calls:
            print(f"  [{self._node}]   (no tool calls — proceeding to evidence emission)")

    def on_llm_error(
        self,
        error: BaseException,
        **kwargs: Any,
    ) -> None:
        print(f"  [{self._node}] ✗ LLM error: {error}")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

LOGGER = AuditLogger(verbose=True)
