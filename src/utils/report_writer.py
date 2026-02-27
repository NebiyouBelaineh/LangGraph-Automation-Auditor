"""Markdown serialization for AuditReport.

Produces a human-readable Markdown document from the Chief Justice's AuditReport
following the structure required by the challenge spec:

    # Audit Report: <repo_url>
    ## Executive Summary
    ## Criterion Breakdown
      ### 1. <name> — Score: <score>/5
         Prosecutor / Defense / Tech Lead opinions
         Dissent (when variance > 2)
         Remediation
    ## Remediation Plan
"""

from __future__ import annotations

from src.state import AuditReport, CriterionResult, JudicialOpinion


def _opinion_line(op: JudicialOpinion) -> str:
    cited = ", ".join(op.cited_evidence) if op.cited_evidence else "none"
    return f"**{op.judge}** (score={op.score}/5) — {op.argument}\n  *Cited:* {cited}"


def _criterion_section(index: int, c: CriterionResult) -> str:
    lines: list[str] = [
        f"### {index}. {c.dimension_name} — Score: {c.final_score}/5",
        "",
    ]

    # Opinions — preserve canonical order: Prosecutor → Defense → TechLead
    ordered_judges = ["Prosecutor", "Defense", "TechLead"]
    opinion_map = {op.judge: op for op in c.judge_opinions}
    for judge in ordered_judges:
        if judge in opinion_map:
            lines.append(_opinion_line(opinion_map[judge]))
            lines.append("")

    if c.dissent_summary:
        lines.append(f"**Dissent:** {c.dissent_summary}")
        lines.append("")

    lines.append(f"**Remediation:** {c.remediation}")
    lines.append("")
    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def audit_report_to_markdown(report: AuditReport) -> str:
    """Convert an AuditReport Pydantic model to a Markdown string."""
    parts: list[str] = [
        f"# Audit Report: {report.repo_url}",
        "",
        "## Executive Summary",
        "",
        f"**Overall Score: {report.overall_score:.2f} / 5.00**",
        "",
        report.executive_summary,
        "",
        "---",
        "",
        "## Criterion Breakdown",
        "",
    ]

    for i, criterion in enumerate(report.criteria, 1):
        parts.append(_criterion_section(i, criterion))

    parts += [
        "## Remediation Plan",
        "",
        report.remediation_plan,
    ]

    return "\n".join(parts)
