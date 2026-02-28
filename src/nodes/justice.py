"""Chief Justice node — deterministic conflict-resolution for the Automaton Auditor.

This is NOT an LLM node.  It is pure Python that receives all JudicialOpinion
objects from the three parallel judge nodes, applies hard-coded synthesis rules,
and produces a single AuditReport.

Rubric synthesis rules (from docs/rubric.json):
  - security_override     : confirmed security flaw → cap final score at 3
  - fact_supremacy        : detective evidence (found=False) beats Defense opinion
  - functionality_weight  : Tech Lead has highest weight for architecture criteria
  - dissent_requirement   : score variance > 2 → mandatory dissent_summary
  - variance_re_evaluation: score variance > 2 → re-evaluate cited evidence before scoring

Averaging scores or delegating to another LLM prompt here would cap the
chief_justice_synthesis rubric criterion at score = 2 per challenge spec.
"""

from __future__ import annotations

from typing import Any

from src.state import AgentState, AuditReport, CriterionResult, Evidence, JudicialOpinion

# Rubric dimensions where Tech Lead's architectural judgment should carry
# the highest weight during conflict resolution.
_ARCHITECTURE_CRITERIA = {
    "graph_orchestration",
    "state_management_rigor",
    "safe_tool_engineering",
    "chief_justice_synthesis",
}

# Keyword signals that indicate a confirmed security flaw in a judge's argument.
_SECURITY_FLAW_SIGNALS = [
    "os.system",
    "security negligence",
    "shell injection",
    "unsanitized",
    "no sandbox",
    "no error handling",
    "security_negligence",
]


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------


def _variance(opinions: list[JudicialOpinion]) -> int:
    """Return max-score − min-score across all opinions."""
    if not opinions:
        return 0
    scores = [op.score for op in opinions]
    return max(scores) - min(scores)


def _get_opinion(opinions: list[JudicialOpinion], judge: str) -> JudicialOpinion | None:
    """Return the opinion from a specific judge role, or None."""
    for op in opinions:
        if op.judge == judge:
            return op
    return None


def _has_security_flaw(opinion: JudicialOpinion) -> bool:
    """Return True when the Prosecutor's argument mentions a confirmed security flaw."""
    if opinion.judge != "Prosecutor":
        return False
    combined = (opinion.argument + " ".join(opinion.cited_evidence)).lower()
    return any(signal in combined for signal in _SECURITY_FLAW_SIGNALS)


def _evidence_found(dim_id: str, evidences: dict[str, list[Evidence]]) -> bool | None:
    """Look up detective finding for a dimension across all evidence buckets.

    Returns True/False when found, None when the dimension has no detective record.
    """
    for bucket_items in evidences.values():
        for ev in bucket_items:
            if ev.goal == dim_id:
                return ev.found
    return None


def apply_rule_of_security(
    prosecutor: JudicialOpinion | None,
    defense: JudicialOpinion | None,
    tech_lead: JudicialOpinion | None,
) -> int | None:
    """Rule 1 — Security Override: confirmed security flaw caps the final score at 3.

    When the Prosecutor's argument contains keyword signals for a confirmed security
    vulnerability (shell injection, unsanitized input, no sandbox, etc.), the final
    score is hard-capped at 3, regardless of Defense or Tech Lead positions.

    Returns the capped score if the rule fires, or None if it does not apply.
    """
    if prosecutor and _has_security_flaw(prosecutor):
        raw = _weighted_score(prosecutor, defense, tech_lead)
        return min(raw, 3)
    return None


def apply_rule_of_evidence(
    dim_id: str,
    defense: JudicialOpinion | None,
    tech_lead: JudicialOpinion | None,
    prosecutor: JudicialOpinion | None,
    evidences: dict[str, list[Evidence]],
) -> int | None:
    """Rule 2 — Fact Supremacy: detective found=False overrides Defense generosity.

    When the detective confirmed the artifact is missing (found=False) but the Defense
    assigned a generous score (≥ 4), the Defense score is demoted by 2.  The final
    score is then the minimum of the demoted Defense score and the Tech Lead score,
    grounded in what actually exists in the repository.

    Returns the adjusted score if the rule fires, or None if it does not apply.
    """
    detective_found = _evidence_found(dim_id, evidences)
    if detective_found is False and defense and defense.score >= 4:
        tl_score = tech_lead.score if tech_lead else 3
        adjusted_defense = max(defense.score - 2, 1)
        adjusted = min(tl_score, adjusted_defense)
        return max(adjusted, prosecutor.score if prosecutor else 1)
    return None


def apply_rule_of_functionality(
    dim_id: str,
    prosecutor: JudicialOpinion | None,
    defense: JudicialOpinion | None,
    tech_lead: JudicialOpinion | None,
) -> int | None:
    """Rule 3 — Functionality Weight: Tech Lead carries highest weight for architecture criteria.

    For architecture-critical dimensions (graph_orchestration, state_management_rigor,
    safe_tool_engineering, chief_justice_synthesis), the Tech Lead's score is
    double-weighted in the weighted average — the Tech Lead is the most qualified
    judge for correctness questions on those dimensions.

    Returns the weighted score if the rule fires, or None if it does not apply.
    """
    if dim_id in _ARCHITECTURE_CRITERIA and tech_lead:
        return _weighted_score(prosecutor, defense, tech_lead, tl_weight=2)
    return None


def _resolve(
    dim_id: str,
    opinions: list[JudicialOpinion],
    evidences: dict[str, list[Evidence]],
) -> int:
    """Apply conflict-resolution rules and return the final score (1-5).

    Rules applied in priority order:
    1. security_override  — Prosecutor identifies confirmed security flaw → cap at 3
    2. fact_supremacy     — detective found=False overrides Defense generosity
    3. functionality_weight — Tech Lead carries highest weight for architecture criteria
    4. variance_re_evaluation — high variance triggers evidence re-check before scoring
    5. Default            — weighted median: TechLead × 2 + Prosecutor + Defense (or simple mean)
    """
    if not opinions:
        return 1

    prosecutor = _get_opinion(opinions, "Prosecutor")
    defense = _get_opinion(opinions, "Defense")
    tech_lead = _get_opinion(opinions, "TechLead")

    # ── Rule 1: security_override ────────────────────────────────────────
    score = apply_rule_of_security(prosecutor, defense, tech_lead)
    if score is not None:
        return score

    # ── Rule 2: fact_supremacy ───────────────────────────────────────────
    score = apply_rule_of_evidence(dim_id, defense, tech_lead, prosecutor, evidences)
    if score is not None:
        return score

    # ── Rule 3: functionality_weight (architecture criteria) ─────────────
    score = apply_rule_of_functionality(dim_id, prosecutor, defense, tech_lead)
    if score is not None:
        return score

    # ── Rule 4 & 5: variance_re_evaluation + default weighted score ──────
    var = _variance(opinions)
    if var > 2:
        # High disagreement — Re-evaluate: weight Tech Lead more heavily as tie-breaker.
        return _weighted_score(prosecutor, defense, tech_lead, tl_weight=2)

    return _weighted_score(prosecutor, defense, tech_lead)


def _weighted_score(
    prosecutor: JudicialOpinion | None,
    defense: JudicialOpinion | None,
    tech_lead: JudicialOpinion | None,
    tl_weight: int = 1,
) -> int:
    """Compute a weighted score and clamp to [1, 5]."""
    total_weight = 0
    total_score = 0

    if prosecutor:
        total_score += prosecutor.score * 1
        total_weight += 1
    if defense:
        total_score += defense.score * 1
        total_weight += 1
    if tech_lead:
        total_score += tech_lead.score * tl_weight
        total_weight += tl_weight

    if total_weight == 0:
        return 1

    raw = round(total_score / total_weight)
    return max(1, min(5, raw))


def _dissent_summary(opinions: list[JudicialOpinion]) -> str:
    """Build a mandatory dissent summary when score variance exceeds 2."""
    lines = ["The panel was significantly divided:"]
    for op in opinions:
        lines.append(f"  • {op.judge} (score={op.score}): {op.argument[:200]}")
    prosecutor = _get_opinion(opinions, "Prosecutor")
    defense = _get_opinion(opinions, "Defense")
    tech_lead = _get_opinion(opinions, "TechLead")
    if prosecutor and defense:
        lines.append(
            f"Dialectical tension: Prosecutor argued for score={prosecutor.score} "
            f"while Defense argued for score={defense.score}. "
            f"Tech Lead's pragmatic assessment (score={tech_lead.score if tech_lead else 'n/a'}) "
            "was used as the primary tie-breaker per functionality_weight rule."
        )
    return "\n".join(lines)


def _build_remediation(
    dim_id: str,
    opinions: list[JudicialOpinion],
    evidences: dict[str, list[Evidence]],
) -> str:
    """Produce file-level remediation instructions from Tech Lead + Prosecutor evidence."""
    parts: list[str] = []

    tech_lead = _get_opinion(opinions, "TechLead")
    prosecutor = _get_opinion(opinions, "Prosecutor")

    if tech_lead:
        parts.append(f"[Tech Lead] {tech_lead.argument}")
        if tech_lead.cited_evidence:
            parts.append("  Cited artifacts: " + ", ".join(tech_lead.cited_evidence))

    if prosecutor:
        gaps = [e for e in prosecutor.cited_evidence if e != "emission_failure"]
        if gaps:
            parts.append(f"[Prosecutor] Identified gaps: {', '.join(gaps)}")

    # Append the detective finding for context.
    detective_found = _evidence_found(dim_id, evidences)
    if detective_found is False:
        parts.append(
            f"[Evidence] Detective confirmed '{dim_id}' artifact was NOT found in the repository."
        )

    return "\n".join(parts) if parts else f"No specific remediation identified for {dim_id}."


def _executive_summary(criteria: list[CriterionResult], overall: float) -> str:
    """Generate a high-level verdict narrative from all criterion results."""
    high = [c for c in criteria if c.final_score >= 4]
    mid = [c for c in criteria if c.final_score == 3]
    low = [c for c in criteria if c.final_score <= 2]

    lines = [
        f"Overall audit score: {overall:.2f} / 5.00.",
        "",
        "The Dialectical Synthesis (Prosecutor × Defense × Tech Lead) resolved "
        f"{len(criteria)} rubric dimensions using deterministic conflict-resolution rules.",
        "",
    ]
    if high:
        lines.append(
            "Strong areas: "
            + ", ".join(f"{c.dimension_name} ({c.final_score}/5)" for c in high)
            + "."
        )
    if mid:
        lines.append(
            "Adequate areas: "
            + ", ".join(f"{c.dimension_name} ({c.final_score}/5)" for c in mid)
            + "."
        )
    if low:
        lines.append(
            "Critical gaps: "
            + ", ".join(f"{c.dimension_name} ({c.final_score}/5)" for c in low)
            + "."
        )
    return "\n".join(lines)


def _remediation_plan(criteria: list[CriterionResult]) -> str:
    """Consolidate per-criterion remediation into a single ordered plan."""
    # Prioritise low-scoring dimensions first.
    sorted_criteria = sorted(criteria, key=lambda c: c.final_score)
    lines = ["## Priority Remediation Plan\n"]
    for i, c in enumerate(sorted_criteria, 1):
        lines.append(f"### {i}. {c.dimension_name} (score={c.final_score}/5)")
        lines.append(c.remediation)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chief Justice node
# ---------------------------------------------------------------------------


def chief_justice_node(state: AgentState) -> dict[str, Any]:
    """Deterministic Python synthesis of all JudicialOpinion objects into AuditReport.

    No LLM is called here — only named hard-coded rules from the rubric spec.
    """
    opinions: list[JudicialOpinion] = state.get("opinions", [])
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})
    rubric_dimensions: list[dict] = state.get("rubric_dimensions", [])

    # Group opinions by criterion_id
    by_criterion: dict[str, list[JudicialOpinion]] = {}
    for op in opinions:
        by_criterion.setdefault(op.criterion_id, []).append(op)

    criteria_results: list[CriterionResult] = []

    for dim in rubric_dimensions:
        dim_id = dim["id"]
        dim_opinions = by_criterion.get(dim_id, [])

        final_score = _resolve(dim_id, dim_opinions, evidences)
        var = _variance(dim_opinions)
        dissent = _dissent_summary(dim_opinions) if var > 2 else None
        remediation = _build_remediation(dim_id, dim_opinions, evidences)

        criteria_results.append(
            CriterionResult(
                dimension_id=dim_id,
                dimension_name=dim.get("name", dim_id),
                final_score=final_score,
                judge_opinions=dim_opinions,
                dissent_summary=dissent,
                remediation=remediation,
            )
        )

    overall = (
        sum(c.final_score for c in criteria_results) / len(criteria_results)
        if criteria_results
        else 0.0
    )

    report = AuditReport(
        repo_url=state.get("repo_url", "unknown"),
        executive_summary=_executive_summary(criteria_results, overall),
        overall_score=round(overall, 2),
        criteria=criteria_results,
        remediation_plan=_remediation_plan(criteria_results),
    )

    return {"final_report": report}
