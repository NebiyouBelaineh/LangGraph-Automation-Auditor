"""CLI entrypoint for the Automaton Auditor.

Usage:
    # Full run (detective → judicial → chief-justice):
    uv run python main.py --repo-url https://github.com/... --pdf-path reports/interim_report.pdf

    # Skip detective stage — feed saved evidence straight into the judicial layer:
    uv run python main.py --evidence-path output/evidence_20260228_030516.json

    # Suppress the real-time agent trace (only the final summary is printed):
    uv run python main.py --repo-url ... --pdf-path ... --quiet

    # Resume a crashed run (reuses checkpoint; skips completed nodes):
    uv run python main.py --repo-url ... --pdf-path ... --thread-id audit-20260227-165030
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_RUBRIC_PATH = Path(__file__).parent / "docs" / "rubric.json"


def _load_rubric() -> list[dict]:
    """Load rubric dimensions from rubric.json; returns empty list if file is missing."""
    if not _RUBRIC_PATH.exists():
        print(f"[auditor] Warning: rubric not found at {_RUBRIC_PATH}", file=sys.stderr)
        return []
    with _RUBRIC_PATH.open() as fh:
        data = json.load(fh)
    return data.get("dimensions", [])


def _load_evidence(path: Path) -> dict:
    """Load a saved evidence JSON and return {bucket: [Evidence, ...]} dict.

    Raises SystemExit on any parse / validation error so the caller stays clean.
    """
    from src.state import Evidence  # local import — keeps startup fast

    if not path.exists():
        print(f"[error] Evidence file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"[error] Evidence file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    raw_evidences = raw.get("evidences")
    if not isinstance(raw_evidences, dict):
        print("[error] Evidence file has no top-level 'evidences' dict.", file=sys.stderr)
        sys.exit(1)

    evidences: dict = {}
    for bucket, items in raw_evidences.items():
        try:
            evidences[bucket] = [Evidence(**item) for item in items]
        except Exception as exc:
            print(f"[error] Could not parse evidences[{bucket!r}]: {exc}", file=sys.stderr)
            sys.exit(1)

    return evidences


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automaton-auditor",
        description="LangGraph swarm that audits a GitHub repo + PDF report.",
    )
    parser.add_argument(
        "--evidence-path",
        default=None,
        metavar="PATH",
        help=(
            "Path to a previously saved evidence JSON file. "
            "When given, the detective stage is skipped and the run starts "
            "directly at the judicial layer. "
            "--repo-url and --pdf-path are not required in this mode."
        ),
    )
    parser.add_argument(
        "--repo-url",
        help="GitHub repo URL to audit (https:// or git@)",
    )
    parser.add_argument(
        "--branch",
        "-b",
        default=None,
        metavar="BRANCH",
        help=(
            "Branch (or tag / commit SHA) to clone. "
            "Passed as --branch BRANCH to git clone. "
            "Defaults to the remote HEAD."
        ),
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=50,
        metavar="N",
        help=(
            "Shallow-clone depth passed as --depth N to git clone (default: 50). "
            "Use 0 to perform a full clone."
        ),
    )
    parser.add_argument(
        "--pdf-path",
        help="Path to the PDF report to analyse (inside container if using Docker)",
    )
    parser.add_argument(
        "--output",
        default="output/evidence.json",
        help="Where to write the evidence JSON (default: output/evidence.json)",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        metavar="ID",
        help=(
            "Resume a previous run using its checkpoint thread ID. "
            "Completed nodes are skipped; only failed/unstarted nodes re-run. "
            "The thread ID is printed at the start of every run."
        ),
    )
    parser.add_argument(
        "--help-docker",
        action="store_true",
        help="Print Docker run example with volume mounts and exit",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        default=True,
        help="Print real-time agent trace to stdout (default: on)",
    )
    verbosity.add_argument(
        "--quiet",
        dest="verbose",
        action="store_false",
        help="Suppress the real-time agent trace (only final summary is shown)",
    )
    return parser


def _print_docker_help() -> None:
    print(
        """
The PDF path is resolved inside the container. Mount your report directory
and use the path as seen inside the container.

Example (Linux/macOS):

  docker run --rm \\
    -v /path/on/host/to/reports:/app/reports \\
    -v /path/on/host/to/output:/app/output \\
    automaton-auditor:dev \\
    --repo-url https://github.com/owner/repo \\
    --pdf-path /app/reports/interim_report.pdf \\
    --output /app/output/evidence.json

Replace /path/on/host/to/reports with the directory containing your PDF
(e.g. docs/report). Replace /path/on/host/to/output with where you want
evidence.json written (or omit the output volume and use the default).
"""
    )


def _write_report_md(report, path: Path) -> None:
    """Render an AuditReport as a Markdown file."""
    lines: list[str] = [
        "# Automaton Auditor — Final Report",
        "",
        f"**Repo:** {report.repo_url}",
        f"**Overall score:** {report.overall_score:.2f} / 5.00",
        "",
        "## Executive Summary",
        "",
        report.executive_summary,
        "",
        "## Criterion Scores",
        "",
        "| Dimension | Score | Dissent |",
        "|-----------|-------|---------|",
    ]
    for c in report.criteria:
        dissent_flag = "⚖ yes" if c.dissent_summary else "—"
        lines.append(f"| {c.dimension_name} | {c.final_score}/5 | {dissent_flag} |")

    lines += ["", "## Per-Criterion Detail", ""]
    for c in report.criteria:
        lines += [
            f"### {c.dimension_name} — {c.final_score}/5",
            "",
        ]
        for op in c.judge_opinions:
            lines += [
                f"**{op.judge}** (score {op.score}): {op.argument}",
                "",
                f"*Cited:* {', '.join(op.cited_evidence)}",
                "",
            ]
        if c.dissent_summary:
            lines += [f"> **Dissent:** {c.dissent_summary}", ""]
        lines += [f"**Remediation:** {c.remediation}", ""]

    lines += ["## Priority Remediation Plan", "", report.remediation_plan]
    path.write_text("\n".join(lines))


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if getattr(args, "help_docker", False):
        _print_docker_help()
        sys.exit(0)

    from langgraph.checkpoint.sqlite import SqliteSaver

    from src.graph import make_graph
    from src.utils.audit_logger import LOGGER
    from src.utils.spend_tracker import TRACKER

    LOGGER.verbose = args.verbose

    rubric_dimensions = _load_rubric()
    print(f"[auditor] Loaded {len(rubric_dimensions)} rubric dimensions from {_RUBRIC_PATH.name}")

    # Thread ID for checkpointing
    thread_id: str = args.thread_id or (
        f"audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    resuming = args.thread_id is not None
    print(f"[auditor] Thread ID : {thread_id}")

    base = Path(args.output)
    base.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_db = base.parent / "checkpoints.db"
    print(f"[auditor] Checkpoint: {checkpoint_db}")

    # ------------------------------------------------------------------ #
    # Mode A: resume from saved evidence — skip detective stage entirely   #
    # ------------------------------------------------------------------ #
    if args.evidence_path:
        evidence_path = Path(args.evidence_path)
        preloaded_evidences = _load_evidence(evidence_path)
        print(f"[auditor] Mode      : FROM-EVIDENCE  (detectives skipped)")
        print(f"[auditor] Evidence  : {evidence_path}  "
              f"({sum(len(v) for v in preloaded_evidences.values())} items across "
              f"{len(preloaded_evidences)} buckets)")

        initial_state = {
            "repo_url": args.repo_url or "",
            "pdf_path": args.pdf_path or "",
            "rubric_dimensions": rubric_dimensions,
            "clone_options": {},
            "skip_detectives": True,
            "evidences": preloaded_evidences,
            "opinions": [],
            "final_report": None,
        }

    # ------------------------------------------------------------------ #
    # Mode B: full run — detective → judicial → chief-justice              #
    # ------------------------------------------------------------------ #
    else:
        if not args.repo_url or not args.pdf_path:
            parser.error("--repo-url and --pdf-path are required unless --evidence-path is given")

        pdf_path = Path(args.pdf_path)
        if not pdf_path.exists():
            print(f"[error] PDF not found: {pdf_path}", file=sys.stderr)
            sys.exit(1)

        if resuming:
            print("[auditor] Mode      : RESUME  (completed nodes will be skipped)")
        else:
            print("[auditor] Mode      : NEW RUN")
        print(f"[auditor] Tip       : re-run with --thread-id {thread_id} if the process crashes")

        clone_options: dict = {}
        if args.branch:
            clone_options["branch"] = args.branch
        if args.depth != 50:
            clone_options["depth"] = args.depth

        print(f"[auditor] Running graph for repo: {args.repo_url}")
        if args.branch:
            print(f"[auditor] Branch    : {args.branch}")
        depth_display = "full clone" if args.depth == 0 else f"--depth {args.depth}"
        print(f"[auditor] Clone     : {depth_display}")
        print(f"[auditor] PDF       : {pdf_path}")

        initial_state = {
            "repo_url": args.repo_url,
            "pdf_path": str(pdf_path),
            "rubric_dimensions": rubric_dimensions,
            "clone_options": clone_options,
            "skip_detectives": False,
            "evidences": {},
            "opinions": [],
            "final_report": None,
        }

    run_config = {"configurable": {"thread_id": thread_id}}

    with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        graph = make_graph(checkpointer=checkpointer)
        result = graph.invoke(initial_state, config=run_config)

    evidences = result.get("evidences", {})
    final_report = result.get("final_report")

    # ── Evidence summary ───────────────────────────────────────────────────
    print("\n── Evidence Summary ─────────────────────────────────────────────")
    for bucket, items in evidences.items():
        print(f"\n[{bucket.upper()}]")
        for ev in items:
            status = "✓" if ev.found else "✗"
            print(f"  {status} {ev.goal:<35} confidence={ev.confidence:.2f}")
            if ev.rationale:
                short = ev.rationale[:120].replace("\n", " ")
                print(f"      {short}")

    # ── Audit report summary ───────────────────────────────────────────────
    if final_report:
        print("\n── Audit Report ─────────────────────────────────────────────────")
        print(f"  Overall score : {final_report.overall_score:.2f} / 5.00")
        print(f"\n{final_report.executive_summary}")
        print("\n  Per-dimension scores:")
        for c in final_report.criteria:
            bar = "█" * c.final_score + "░" * (5 - c.final_score)
            print(f"  [{bar}] {c.final_score}/5  {c.dimension_name}")
            if c.dissent_summary:
                print(f"         ⚖  Dissent: {c.dissent_summary[:120].replace(chr(10), ' ')}")
    else:
        print("\n[auditor] Warning: no final_report in result — judicial layer may not have run.")

    print(f"\n{TRACKER.report()}")

    # ── Persist outputs ────────────────────────────────────────────────────
    # Always write a timestamped evidence file; also update the canonical
    # path (--output) so the caller always has a fixed reference.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_path = base.parent / f"{base.stem}_{timestamp}{base.suffix}"

    serialisable: dict = {
        "thread_id": thread_id,
        "evidences": {
            bucket: [ev.model_dump() for ev in items]
            for bucket, items in evidences.items()
        },
        "spend": TRACKER.summary(),
    }
    if final_report:
        serialisable["final_report"] = final_report.model_dump()

    payload = json.dumps(serialisable, indent=2)
    timestamped_path.write_text(payload)
    base.write_text(payload)
    print(f"[auditor] Evidence written to {timestamped_path}")
    print(f"[auditor] Latest snapshot : {base}")

    # Write the human-readable audit report as a separate markdown file.
    if final_report:
        report_path = base.parent / f"audit_report_{timestamp}.md"
        _write_report_md(final_report, report_path)
        print(f"[auditor] Audit report    : {report_path}")


if __name__ == "__main__":
    main()
