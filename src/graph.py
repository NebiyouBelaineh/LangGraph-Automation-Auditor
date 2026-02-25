"""LangGraph graph definition for the Automaton Auditor (First stage).

Topology:
    START → entry_node
    entry_node → [repo_investigator, doc_analyst, vision_inspector]  (fan-out)
    [repo_investigator, doc_analyst, vision_inspector] → evidence_aggregator  (fan-in)
    evidence_aggregator → END

Conditional edges: entry_node routes to END early when inputs are missing/invalid.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from src.nodes.detectives import (
    doc_analyst_node,
    repo_investigator_node,
    vision_inspector_node,
)
from src.state import AgentState, Evidence


# ---------------------------------------------------------------------------
# Helper nodes
# ---------------------------------------------------------------------------


def entry_node(state: AgentState) -> dict[str, Any]:
    """Validate inputs and initialise evidences dict.

    Returns partial state.  The conditional router below decides whether
    to proceed to the detective branches or abort early.
    """
    repo_url: str = state.get("repo_url", "").strip()
    pdf_path: str = state.get("pdf_path", "").strip()

    evidences: dict[str, list[Evidence]] = state.get("evidences", {})

    # Normalise: ensure all detective buckets exist (empty lists are fine at this stage)
    for bucket in ("repo", "doc", "vision"):
        evidences.setdefault(bucket, [])

    return {
        "repo_url": repo_url,
        "pdf_path": pdf_path,
        "evidences": evidences,
    }


_REQUIRED_DIMENSIONS = {
    "repo": ["git_forensic_analysis", "state_management_rigor", "graph_orchestration", "safe_tool_engineering"],
    "doc": ["theoretical_depth", "report_accuracy"],
    "vision": ["swarm_visual"],
}


def evidence_aggregator_node(state: AgentState) -> dict[str, Any]:
    """Validate all dimension keys are present; backfill missing with found=False Evidence.
    """
    evidences: dict[str, list[Evidence]] = dict(state.get("evidences", {}))

    for bucket, dimensions in _REQUIRED_DIMENSIONS.items():
        bucket_evidences = {e.goal: e for e in evidences.get(bucket, [])}
        for dim in dimensions:
            if dim not in bucket_evidences:
                bucket_evidences[dim] = Evidence(
                    goal=dim,
                    found=False,
                    location="unknown",
                    rationale="No evidence collected — detective node did not emit this dimension.",
                    confidence=0.0,
                )
        evidences[bucket] = list(bucket_evidences.values())

    return {"evidences": evidences}


# ---------------------------------------------------------------------------
# Conditional router
# ---------------------------------------------------------------------------


def _input_router(state: AgentState) -> Literal["detectives", "abort"]:
    """Route to detective fan-out or abort early on missing inputs."""
    repo_url = state.get("repo_url", "").strip()
    pdf_path = state.get("pdf_path", "").strip()

    if not repo_url or not pdf_path:
        return "abort"
    if not (repo_url.startswith("https://") or repo_url.startswith("git@")):
        return "abort"
    return "detectives"


def _abort_node(state: AgentState) -> dict[str, Any]:
    """Emit found=False for all dimensions when inputs are invalid.
    """
    all_dimensions = [dim for dims in _REQUIRED_DIMENSIONS.values() for dim in dims]
    evidences: dict[str, list[Evidence]] = {}
    for bucket, dimensions in _REQUIRED_DIMENSIONS.items():
        evidences[bucket] = [
            Evidence(
                goal=dim,
                found=False,
                location="unknown",
                rationale="Aborted: missing or invalid repo_url / pdf_path.",
                confidence=0.0,
            )
            for dim in dimensions
        ]
    return {"evidences": evidences}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

builder = StateGraph(AgentState)

builder.add_node("entry", entry_node)
builder.add_node("repo_investigator", repo_investigator_node)
builder.add_node("doc_analyst", doc_analyst_node)
builder.add_node("vision_inspector", vision_inspector_node)
builder.add_node("evidence_aggregator", evidence_aggregator_node)
builder.add_node("abort", _abort_node)

builder.add_edge(START, "entry")

# Conditional: valid inputs → fan-out; invalid → abort
builder.add_conditional_edges(
    "entry",
    _input_router,
    {
        "detectives": "repo_investigator",
        "abort": "abort",
    },
)
# entry also fans out directly to doc_analyst and vision_inspector
builder.add_edge("entry", "doc_analyst")
builder.add_edge("entry", "vision_inspector")

# Fan-in: all three detectives converge at evidence_aggregator
builder.add_edge("repo_investigator", "evidence_aggregator")
builder.add_edge("doc_analyst", "evidence_aggregator")
builder.add_edge("vision_inspector", "evidence_aggregator")

builder.add_edge("abort", END)
builder.add_edge("evidence_aggregator", END)

graph = builder.compile()
