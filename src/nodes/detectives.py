"""Detective node functions for the Automaton Auditor graph.

Each node is an LLM tool-calling agent that:
1. Reads its assigned rubric dimensions from AgentState.rubric_dimensions.
2. Builds a system prompt from each dimension's forensic_instruction,
   success_pattern, and failure_pattern.
3. Autonomously calls forensic tools until it has enough evidence.
4. Emits a structured Evidence object per dimension via with_structured_output.

Nodes never raise — failures produce Evidence(found=False, ...) entries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from src.state import AgentState, Evidence
from src.utils.audit_logger import LOGGER
from src.utils.spend_tracker import TRACKER
from src.tools.doc_tools import extract_and_check_file_paths, query_pdf_for_term
from src.tools.repo_tools import (
    clone_repo,
    clone_repo_sandboxed,
    list_repo_files,
    read_git_history,
    run_graph_structure_analysis,
    scan_file_for_patterns,
)
from src.tools.vision_tools import extract_and_analyze_diagrams


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

_DETECTIVE_RULES = """\
You are a forensic investigator conducting a software audit.
Your job is to collect factual evidence for each assigned rubric dimension.

RULES:
- Always call tools to gather evidence before drawing any conclusions.
- Do not assume file contents, code structure, or PDF content — verify with tools.
- If a tool returns an error, set found=false for the affected dimension.
- For each dimension you must emit exactly one Evidence object.
- Your rationale must explicitly reference the success or failure pattern from the rubric.
- Confidence (0.0–1.0) must reflect how closely the evidence matches the success pattern:
    1.0 = fully matches success pattern
    0.0 = fully matches failure pattern
    0.5 = partial or ambiguous match

TOOL CALL EFFICIENCY (important):
- Issue ALL independent tool calls simultaneously in a single response turn.
  Do NOT call one tool and wait for its result before calling other tools that
  do not depend on it. For example: if the forensic instruction asks you to
  search for four terms, issue all four query_pdf_for_term calls at once.
- Only issue tool calls sequentially when the second call strictly depends on
  the result of the first (e.g. list_repo_files to discover a filename, then
  scan_file_for_patterns on that specific file).
- This applies regardless of how many checks the rubric requires — always
  batch everything that can run independently into a single turn.
"""


def _build_system_prompt(dimensions: list[dict]) -> str:
    """Construct a system prompt embedding all forensic instructions for assigned dimensions."""
    parts = [_DETECTIVE_RULES, "\n\nYOUR ASSIGNED DIMENSIONS:\n"]
    for dim in dimensions:
        parts.append(
            f"\n--- Dimension: {dim['id']} ---\n"
            f"Name: {dim['name']}\n"
            f"Forensic Instruction:\n  {dim['forensic_instruction']}\n"
            f"Success Pattern:\n  {dim['success_pattern']}\n"
            f"Failure Pattern:\n  {dim['failure_pattern']}\n"
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

import os
from dotenv import load_dotenv

load_dotenv()
_MODEL = os.environ.get("DETECTIVE_MODEL", "claude-haiku-4-5-20251001")
_MAX_ITERATIONS = int(os.environ.get("DETECTIVE_MAX_ITERATIONS", 8))
# Stop early after this many consecutive all-fail rounds (every tool returned
# an error or "file not found") to avoid burning tokens on non-existent files.
_MAX_EMPTY_ROUNDS = 2


def _is_not_found(result: Any) -> bool:
    """Return True when a tool result carries no useful information.

    Matches the common "file not found" / error patterns returned by repo_tools
    and doc_tools so the loop can detect when the LLM is spinning its wheels.
    """
    if not isinstance(result, dict):
        return False
    if result.get("exists") is False:
        return True
    if result.get("ok") is False:
        return True
    return False


def _run_detective_agent(
    dimension: dict,
    user_message: str,
    tools: list,
    node_name: str,
) -> Evidence:
    """Run a tool-calling LLM loop for a single rubric dimension and emit one Evidence.

    Each call handles exactly one dimension so the system prompt stays small,
    failures are isolated per dimension, and spend is tracked per dimension.

    Two cost-control mechanisms are active:
    - missing_paths: if scan_file_for_patterns already confirmed a file is absent,
      subsequent calls for the same path are short-circuited without an LLM turn.
    - consecutive_empty: if every tool in a round returns not-found/error for
      _MAX_EMPTY_ROUNDS rounds in a row, the loop exits and proceeds to emission.
    """
    TRACKER.set_node(f"{node_name}:{dimension['id']}")
    LOGGER.set_node(node_name, [dimension])

    llm = ChatAnthropic(model=_MODEL, temperature=0)
    llm_with_tools = llm.bind_tools(tools)
    tool_map = {t.name: t for t in tools}

    messages: list = [
        SystemMessage(content=_build_system_prompt([dimension])),
        HumanMessage(content=user_message),
    ]

    _callbacks = {"callbacks": [TRACKER, LOGGER]}

    # Paths already confirmed absent — short-circuit repeated scans of the same file.
    _missing_paths: set[str] = set()
    # Counts consecutive rounds where every tool call was non-productive.
    _consecutive_empty = 0

    # Tool-calling loop
    for _ in range(_MAX_ITERATIONS):
        response = llm_with_tools.invoke(messages, config=_callbacks)
        messages.append(response)

        if not response.tool_calls:
            break

        round_results: list[Any] = []

        for tc in response.tool_calls:
            # ── Missing-file short-circuit ────────────────────────────────
            # If the LLM asks to scan a file already confirmed absent, reply
            # immediately without consuming another LLM call or tool invocation.
            if tc["name"] == "scan_file_for_patterns":
                rel_path: str = tc["args"].get("relative_path", "")
                if rel_path and rel_path in _missing_paths:
                    result: Any = {
                        "ok": False,
                        "exists": False,
                        "error": (
                            f"File not found: {rel_path} — already confirmed missing, "
                            "skipping re-check."
                        ),
                    }
                    messages.append(
                        ToolMessage(
                            content=json.dumps(result, default=str),
                            tool_call_id=tc["id"],
                        )
                    )
                    round_results.append(result)
                    continue

            # ── Normal tool invocation ────────────────────────────────────
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                result = {"error": f"unknown tool: {tc['name']}"}
            else:
                try:
                    result = tool_fn.invoke(tc["args"], config=_callbacks)
                except Exception as exc:
                    result = {"error": str(exc)}

            # Record confirmed-missing paths for future short-circuiting.
            if tc["name"] == "scan_file_for_patterns" and isinstance(result, dict):
                if not result.get("exists", True) or not result.get("ok", True):
                    rp = tc["args"].get("relative_path", "")
                    if rp:
                        _missing_paths.add(rp)

            messages.append(
                ToolMessage(
                    content=json.dumps(result, default=str),
                    tool_call_id=tc["id"],
                )
            )
            round_results.append(result)

        # ── Early-exit on consecutive empty rounds ────────────────────────
        # If every tool call in this round was non-productive, increment the
        # counter.  After _MAX_EMPTY_ROUNDS consecutive such rounds, inject a
        # stop hint and break — the target files simply don't exist.
        if round_results and all(_is_not_found(r) for r in round_results):
            _consecutive_empty += 1
            if _consecutive_empty >= _MAX_EMPTY_ROUNDS:
                messages.append(
                    HumanMessage(
                        content=(
                            "All recent tool calls returned 'file not found' or errors. "
                            "The files you are looking for do not exist in this repository. "
                            "You now have sufficient information — proceed to emit your evidence."
                        )
                    )
                )
                break
        else:
            _consecutive_empty = 0

    # Structured evidence emission — use a fresh LLM instance to avoid conflict
    # between the tool-calling schema and the structured-output schema.
    _EMISSION_PROMPT = (
        f"Based on all evidence gathered, produce one Evidence object "
        f"for dimension '{dimension['id']}'. "
        "Return a JSON object with EXACTLY these fields:\n"
        "  goal       : string — the dimension id\n"
        "  found      : boolean — true if the success pattern is satisfied\n"
        "  location   : string — file path, section, or 'unknown'\n"
        "  rationale  : string — your reasoning citing the success or failure pattern\n"
        "  confidence : float  — a number between 0.0 and 1.0 (NOT inside rationale)\n"
        "IMPORTANT: 'confidence' MUST be a separate numeric field, never embedded in rationale."
    )

    messages.append(HumanMessage(content=_EMISSION_PROMPT))

    evidence_llm = ChatAnthropic(model=_MODEL, temperature=0).with_structured_output(Evidence)
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            retry_msgs = list(messages)
            if attempt > 0:
                retry_msgs.append(
                    HumanMessage(
                        content=(
                            f"Attempt {attempt} failed schema validation: {last_exc}. "
                            "Ensure 'confidence' is a standalone float field "
                            "(e.g. 0.75) — do NOT embed it inside the 'rationale' string."
                        )
                    )
                )
            ev = evidence_llm.invoke(retry_msgs, config=_callbacks)
            LOGGER.log_evidence([ev])
            return ev
        except Exception as exc:
            last_exc = exc
            continue

    fallback = Evidence(
        goal=dimension["id"],
        found=False,
        location="unknown",
        rationale=f"Evidence emission failed after 3 attempts: {last_exc}",
        confidence=0.0,
    )
    LOGGER.log_evidence([fallback])
    return fallback


# ---------------------------------------------------------------------------
# RepoInvestigator
# ---------------------------------------------------------------------------

_REPO_TOOLS = [
    clone_repo,
    read_git_history,
    run_graph_structure_analysis,
    scan_file_for_patterns,
    list_repo_files,
]


def repo_investigator_node(state: AgentState) -> dict[str, Any]:
    """LLM agent that investigates github_repo rubric dimensions using forensic tools.

    Clones the repository once upfront, then runs one LLM agent call per dimension
    so each prompt stays small and a single dimension failure doesn't block the rest.
    """
    repo_url: str = state.get("repo_url", "")
    all_dimensions: list[dict] = state.get("rubric_dimensions", [])
    dimensions = [d for d in all_dimensions if d.get("target_artifact") == "github_repo"]

    if not dimensions:
        return {"evidences": {"repo": []}}

    if not repo_url:
        return {
            "evidences": {
                "repo": [
                    Evidence(
                        goal=d["id"],
                        found=False,
                        location="unknown",
                        rationale="No repo_url provided in state.",
                        confidence=0.0,
                    )
                    for d in dimensions
                ]
            }
        }

    # Clone once upfront — all dimension agents reuse the same cloned_path.
    clone = clone_repo_sandboxed(repo_url)
    if not clone.ok:
        reason = f"Clone failed: {clone.error} — {clone.details}"
        return {
            "evidences": {
                "repo": [
                    Evidence(
                        goal=d["id"],
                        found=False,
                        location="unknown",
                        rationale=reason,
                        confidence=0.0,
                    )
                    for d in dimensions
                ]
            }
        }

    cloned_path = clone.cloned_path
    evidences: list[Evidence] = []

    _repo_tool_names = ", ".join(t.name for t in _REPO_TOOLS)

    for dim in dimensions:
        user_message = (
            f"Audit dimension '{dim['id']}' for repository: {repo_url}\n"
            f"The repository is already cloned at: {cloned_path}\n"
            "Use this cloned_path with the analysis tools. Do NOT call clone_repo again.\n"
            f"You have access to ONLY these tools: {_repo_tool_names}\n"
            "Do NOT attempt to call any tool not listed above."
        )
        try:
            ev = _run_detective_agent(dim, user_message, _REPO_TOOLS, "repo_investigator")
        except Exception as exc:
            ev = Evidence(
                goal=dim["id"],
                found=False,
                location="unknown",
                rationale=f"Agent run failed: {exc}",
                confidence=0.0,
            )
        evidences.append(ev)

    return {"evidences": {"repo": evidences}}


# ---------------------------------------------------------------------------
# DocAnalyst
# ---------------------------------------------------------------------------

_DOC_TOOLS = [query_pdf_for_term, extract_and_check_file_paths]


def doc_analyst_node(state: AgentState) -> dict[str, Any]:
    """LLM agent that investigates pdf_report rubric dimensions by querying PDF content.

    Runs one LLM agent call per dimension so each prompt stays small and
    a single dimension failure doesn't block the rest.
    """
    pdf_path: str = state.get("pdf_path", "")
    all_dimensions: list[dict] = state.get("rubric_dimensions", [])
    dimensions = [d for d in all_dimensions if d.get("target_artifact") == "pdf_report"]

    if not dimensions:
        return {"evidences": {"doc": []}}

    if not pdf_path or not Path(pdf_path).exists():
        return {
            "evidences": {
                "doc": [
                    Evidence(
                        goal=d["id"],
                        found=False,
                        location="unknown",
                        rationale="PDF path not found or not provided.",
                        confidence=0.0,
                    )
                    for d in dimensions
                ]
            }
        }

    evidences: list[Evidence] = []

    _doc_tool_names = ", ".join(t.name for t in _DOC_TOOLS)

    for dim in dimensions:
        user_message = (
            f"Audit dimension '{dim['id']}' using the PDF report: {pdf_path}\n"
            f"You have access to ONLY these tools: {_doc_tool_names}\n"
            "Use query_pdf_for_term to search for relevant terms and concepts. "
            "Use extract_and_check_file_paths to verify any file path claims. "
            "Do NOT attempt to call any tool not listed above."
        )
        try:
            ev = _run_detective_agent(dim, user_message, _DOC_TOOLS, "doc_analyst")
        except Exception as exc:
            ev = Evidence(
                goal=dim["id"],
                found=False,
                location="unknown",
                rationale=f"Agent run failed: {exc}",
                confidence=0.0,
            )
        evidences.append(ev)

    return {"evidences": {"doc": evidences}}


# ---------------------------------------------------------------------------
# VisionInspector
# ---------------------------------------------------------------------------

_VISION_TOOLS = [extract_and_analyze_diagrams]


def _vision_enabled() -> bool:
    """Read VISION_ENABLED at call time so .env changes are always respected."""
    return os.getenv("VISION_ENABLED", "true").lower() not in ("false", "0", "no")


def vision_inspector_node(state: AgentState) -> dict[str, Any]:
    """LLM agent that investigates pdf_images rubric dimensions by analyzing diagrams.

    Filters dimensions with target_artifact='pdf_images' from state, calls the
    diagram extraction and classification tool, then emits structured Evidence.
    Skipped entirely (no LLM or tool calls) when VISION_ENABLED=false.
    """
    # Guard first — no tracker, logger, or LLM calls when disabled.
    if not _vision_enabled():
        all_dimensions: list[dict] = state.get("rubric_dimensions", [])
        dimensions = [d for d in all_dimensions if d.get("target_artifact") == "pdf_images"]
        print("[auditor] VisionInspector disabled (VISION_ENABLED=false) — skipping.")
        return {
            "evidences": {
                "vision": [
                    Evidence(
                        goal=d["id"],
                        found=False,
                        location="unknown",
                        rationale="VisionInspector disabled via VISION_ENABLED=false.",
                        confidence=0.0,
                    )
                    for d in dimensions
                ]
            }
        }

    pdf_path: str = state.get("pdf_path", "")
    all_dimensions = state.get("rubric_dimensions", [])
    dimensions = [d for d in all_dimensions if d.get("target_artifact") == "pdf_images"]

    if not dimensions:
        return {"evidences": {"vision": []}}

    if not pdf_path or not Path(pdf_path).exists():
        return {
            "evidences": {
                "vision": [
                    Evidence(
                        goal=d["id"],
                        found=False,
                        location="unknown",
                        rationale="PDF path not found or not provided.",
                        confidence=0.0,
                    )
                    for d in dimensions
                ]
            }
        }

    evidences: list[Evidence] = []

    _vision_tool_names = ", ".join(t.name for t in _VISION_TOOLS)

    for dim in dimensions:
        user_message = (
            f"Audit dimension '{dim['id']}' using diagrams in the PDF: {pdf_path}\n"
            f"You have access to ONLY these tools: {_vision_tool_names}\n"
            "Call extract_and_analyze_diagrams to extract and classify all diagrams. "
            "Do NOT attempt to call any tool not listed above."
        )
        try:
            ev = _run_detective_agent(dim, user_message, _VISION_TOOLS, "vision_inspector")
        except Exception as exc:
            ev = Evidence(
                goal=dim["id"],
                found=False,
                location="unknown",
                rationale=f"Agent run failed: {exc}",
                confidence=0.0,
            )
        evidences.append(ev)

    return {"evidences": {"vision": evidences}}
