"""CLI entrypoint for the Automaton Auditor.

Usage:
    uv run python main.py --repo-url https://github.com/... --pdf-path reports/interim_report.pdf

    Resume a crashed run (reuses checkpoint; skips completed nodes):
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automaton-auditor",
        description="LangGraph swarm that audits a GitHub repo + PDF report.",
    )
    parser.add_argument(
        "--repo-url",
        help="GitHub repo URL to audit (https:// or git@)",
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


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if getattr(args, "help_docker", False):
        _print_docker_help()
        sys.exit(0)

    if not args.repo_url or not args.pdf_path:
        parser.error("--repo-url and --pdf-path are required (use --help-docker for Docker usage)")

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"[error] PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    from langgraph.checkpoint.sqlite import SqliteSaver

    from src.graph import make_graph
    from src.utils.spend_tracker import TRACKER

    rubric_dimensions = _load_rubric()
    print(f"[auditor] Loaded {len(rubric_dimensions)} rubric dimensions from {_RUBRIC_PATH.name}")

    # Thread ID for checkpointing — every run is identified so state can be resumed after a crash.
    thread_id: str = args.thread_id or (
        f"audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    resuming = args.thread_id is not None
    print(f"[auditor] Thread ID : {thread_id}")
    if resuming:
        print("[auditor] Mode      : RESUME  (completed nodes will be skipped)")
    else:
        print("[auditor] Mode      : NEW RUN")
    print(f"[auditor] Tip       : re-run with --thread-id {thread_id} if the process crashes")

    base = Path(args.output)
    base.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_db = base.parent / "checkpoints.db"
    print(f"[auditor] Checkpoint: {checkpoint_db}")

    initial_state = {
        "repo_url": args.repo_url,
        "pdf_path": str(pdf_path),
        "rubric_dimensions": rubric_dimensions,
        "evidences": {},
        "opinions": [],
        "final_report": None,
    }

    print(f"[auditor] Running graph for repo: {args.repo_url}")
    print(f"[auditor] PDF: {pdf_path}")

    run_config = {"configurable": {"thread_id": thread_id}}

    with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        graph = make_graph(checkpointer=checkpointer)
        result = graph.invoke(initial_state, config=run_config)

    evidences = result.get("evidences", {})

    # Pretty-print to stdout
    print("\n── Evidence Summary ─────────────────────────────────────────────")
    for bucket, items in evidences.items():
        print(f"\n[{bucket.upper()}]")
        for ev in items:
            status = "✓" if ev.found else "✗"
            print(f"  {status} {ev.goal:<35} confidence={ev.confidence:.2f}")
            if ev.rationale:
                short = ev.rationale[:120].replace("\n", " ")
                print(f"      {short}")

    print(f"\n{TRACKER.report()}")

    # Save JSON output — always write a timestamped file; also update the
    # canonical path (--output) so the caller always has a fixed reference.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_path = base.parent / f"{base.stem}_{timestamp}{base.suffix}"

    serialisable = {
        "thread_id": thread_id,
        "evidences": {
            bucket: [ev.model_dump() for ev in items]
            for bucket, items in evidences.items()
        },
        "spend": TRACKER.summary(),
    }
    payload = json.dumps(serialisable, indent=2)
    timestamped_path.write_text(payload)
    base.write_text(payload)
    print(f"[auditor] Evidence written to {timestamped_path}")
    print(f"[auditor] Latest snapshot: {base}")


if __name__ == "__main__":
    main()
