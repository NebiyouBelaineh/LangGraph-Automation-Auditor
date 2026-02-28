"""Judicial layer — three judge nodes for the Automaton Auditor graph.

Each node (prosecutor_node, defense_node, tech_lead_node) runs in parallel
after evidence_aggregator.  For every rubric dimension they receive the same
Evidence and independently produce a JudicialOpinion via structured output.

Parallel execution is visible in the LangSmith trace per the graph topology:
    evidence_aggregator → [prosecutor, defense, tech_lead] → chief_justice

Structured output is mandatory — freeform text triggers a ValidationError.
On failure the node retries up to MAX_RETRIES times then falls back to a
JudicialOpinion with score=1 and an error argument.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from src.state import AgentState, Evidence, JudicialOpinion
from src.utils.spend_tracker import TRACKER

load_dotenv()

_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5-20251001")
_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Artifact → evidence bucket mapping (mirrors graph.py)
# ---------------------------------------------------------------------------

_ARTIFACT_TO_BUCKET: dict[str, str] = {
    "github_repo": "repo",
    "pdf_report": "doc",
    "pdf_images": "vision",
}


def _build_evidence_index(state: AgentState) -> dict[str, Evidence]:
    """Flatten all evidence buckets into a single dimension_id → Evidence lookup."""
    index: dict[str, Evidence] = {}
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})
    for bucket_items in evidences.values():
        for ev in bucket_items:
            index[ev.goal] = ev
    return index


# ---------------------------------------------------------------------------
# System prompt templates — one per persona
# ---------------------------------------------------------------------------

_PROSECUTOR_SYSTEM = """\
You are the PROSECUTOR in a rigorous software audit tribunal.

MANDATE (Protocol B — Statute of Orchestration and Statute of Engineering):
- Assume vibe coding. Trust no one. Assume every claim is a lie until artifacts prove otherwise.
- Your job is to argue for the LOWEST defensible score on each rubric dimension.
- Scrutinize gaps, security flaws, missing implementations, and lazy shortcuts.
- If there is any ambiguity, rule against the defendant.

CHARGES YOU MUST INVESTIGATE:
- "Orchestration Fraud": linear graph flow where parallelism was required → max score = 1 for graph_orchestration.
- "Hallucination Liability": judge nodes returning freeform text with no Pydantic validation → max score = 2 for judicial_nuance.
- "Security Negligence": unsanitized os.system() calls, no sandboxing, no error handling.
- "Report Hallucination": files/features the PDF claims exist that the repo does not have.

OUTPUT RULES:
- Produce a harsh score (typically 1–2 for violations, only go higher if evidence is overwhelming).
- List specific missing elements as cited_evidence.
- Argument must reference exact failure evidence, not vague assertions.
- You MUST produce a JudicialOpinion with fields: judge="Prosecutor", criterion_id, score (1-5), argument, cited_evidence (list of strings).
"""

_DEFENSE_SYSTEM = """\
You are the DEFENSE ATTORNEY in a rigorous software audit tribunal.

MANDATE (Protocol B — Statute of Effort):
- Reward effort and intent. Look for the spirit of the law, not just the letter.
- Your job is to argue for the HIGHEST defensible score on each rubric dimension.
- Highlight creative workarounds, git history showing iteration, architectural understanding
  even when implementation has bugs or gaps.

MITIGATING FACTORS TO IDENTIFY:
- Graph fails to compile due to minor edge error, but AST parsing logic is sophisticated →
  argue for score = 3 for safe_tool_engineering despite broken graph.
- Chief Justice synthesis node is LLM-prompt but judge personas are genuinely distinct →
  argue for score 3-4 for judicial_nuance ("Role Separation achieved dialectical tension").
- Git history shows meaningful struggle and iteration (many commits) →
  argue for higher score on git_forensic_analysis.

OUTPUT RULES:
- Produce a generous score (typically 3–5), only go lower if evidence is catastrophic.
- Highlight specific strengths and mitigating circumstances in cited_evidence.
- You MUST produce a JudicialOpinion with fields: judge="Defense", criterion_id, score (1-5), argument, cited_evidence (list of strings).
"""

_TECH_LEAD_SYSTEM = """\
You are the TECH LEAD in a rigorous software audit tribunal.

MANDATE (Protocol B — Statute of Engineering):
- Evaluate architectural soundness and practical viability only.
- Ignore vibe. Ignore struggle. Focus exclusively on the artifacts.
- You are the tie-breaker between Prosecutor and Defense.
- Assign only scores 1, 3, or 5 — no 2 or 4.

TECHNICAL EVALUATION CRITERIA:
- operator.add / operator.ior reducers used to prevent state overwrites → confirms architectural rigor.
- tempfile.TemporaryDirectory() used for cloning → confirms sandboxing standard.
- Plain Python dicts for state (no Pydantic) → ruling: "Technical Debt" → score = 3 (works but brittle).
- os.system('git clone <url>') with no sandbox → ruling: "Security Negligence" → overrides all effort
  points; cap score at 1 for forensic accuracy.

OUTPUT RULES:
- Produce scores 1, 3, or 5 ONLY (never 2 or 4).
- Argument must include specific technical remediation advice (file-level instructions when possible).
- cited_evidence must reference specific file paths or code constructs.
- You MUST produce a JudicialOpinion with fields: judge="TechLead", criterion_id, score (1-5), argument, cited_evidence (list of strings).
"""


# ---------------------------------------------------------------------------
# Core judge invocation helper
# ---------------------------------------------------------------------------

def _invoke_judge(
    persona: str,
    system_prompt: str,
    dimension: dict,
    evidence: Evidence,
    judge_llm: Any,
) -> JudicialOpinion:
    """Call the judge LLM for a single dimension and return a JudicialOpinion.

    Retries up to _MAX_RETRIES on ValidationError.  Falls back to a score-1
    opinion on total failure so the graph never stalls.
    """
    evidence_json = json.dumps(evidence.model_dump(), indent=2, default=str)
    user_content = (
        f"DIMENSION TO JUDGE:\n"
        f"  id: {dimension['id']}\n"
        f"  name: {dimension.get('name', dimension['id'])}\n"
        f"  success_pattern: {dimension.get('success_pattern', 'n/a')}\n"
        f"  failure_pattern: {dimension.get('failure_pattern', 'n/a')}\n"
        f"  forensic_instruction: {dimension.get('forensic_instruction', 'n/a')}\n\n"
        f"DETECTIVE EVIDENCE:\n{evidence_json}\n\n"
        f"Produce your JudicialOpinion for criterion_id='{dimension['id']}'."
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    _callbacks = {"callbacks": [TRACKER]}

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            retry_msgs = list(messages)
            if attempt > 0:
                retry_msgs.append(
                    HumanMessage(
                        content=(
                            f"Attempt {attempt} failed validation: {last_exc}. "
                            "Return a valid JudicialOpinion JSON with all required fields: "
                            "judge, criterion_id, score (int 1-5), argument (str), "
                            "cited_evidence (list of strings)."
                        )
                    )
                )
            opinion: JudicialOpinion = judge_llm.invoke(retry_msgs, config=_callbacks)
            return opinion
        except (ValidationError, Exception) as exc:
            last_exc = exc

    judge_literal = {"Prosecutor": "Prosecutor", "Defense": "Defense", "TechLead": "TechLead"}[persona]
    return JudicialOpinion(
        judge=judge_literal,
        criterion_id=dimension["id"],
        score=1,
        argument=f"Opinion emission failed after {_MAX_RETRIES} attempts: {last_exc}",
        cited_evidence=["emission_failure"],
    )


# ---------------------------------------------------------------------------
# Generic judge node factory
# ---------------------------------------------------------------------------

def _judge_node(
    persona: str,
    system_prompt: str,
    state: AgentState,
) -> dict[str, Any]:
    """Shared implementation for all three judge nodes.

    Iterates over all rubric dimensions, finds matching evidence, and produces
    one JudicialOpinion per dimension.  Returns {"opinions": [...]}.
    """
    rubric_dimensions: list[dict] = state.get("rubric_dimensions", [])
    evidence_index = _build_evidence_index(state)

    node_label = f"judge:{persona.lower()}"
    TRACKER.set_node(node_label)

    llm = ChatAnthropic(model=_MODEL, temperature=0)
    judge_llm = llm.with_structured_output(JudicialOpinion)

    opinions: list[JudicialOpinion] = []

    for dim in rubric_dimensions:
        dim_id = dim["id"]
        evidence = evidence_index.get(
            dim_id,
            Evidence(
                goal=dim_id,
                found=False,
                location="unknown",
                rationale="No evidence collected for this dimension.",
                confidence=0.0,
            ),
        )
        opinion = _invoke_judge(persona, system_prompt, dim, evidence, judge_llm)
        opinions.append(opinion)

    return {"opinions": opinions}


# ---------------------------------------------------------------------------
# Public judge nodes — three separate LangGraph nodes for parallel execution
# ---------------------------------------------------------------------------


def prosecutor_node(state: AgentState) -> dict[str, Any]:
    """Prosecutor judge: argues for lowest defensible score across all dimensions."""
    return _judge_node("Prosecutor", _PROSECUTOR_SYSTEM, state)


def defense_node(state: AgentState) -> dict[str, Any]:
    """Defense attorney judge: argues for highest defensible score across all dimensions."""
    return _judge_node("Defense", _DEFENSE_SYSTEM, state)


def tech_lead_node(state: AgentState) -> dict[str, Any]:
    """Tech lead judge: evaluates architectural soundness; scores only 1, 3, or 5."""
    return _judge_node("TechLead", _TECH_LEAD_SYSTEM, state)
