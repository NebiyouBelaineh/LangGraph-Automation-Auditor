"""CLI entrypoint for the Automaton Auditor.

Usage:
    uv run python main.py --repo-url https://github.com/... --pdf-path reports/interim_report.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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

    from src.graph import graph

    initial_state = {
        "repo_url": args.repo_url,
        "pdf_path": str(pdf_path),
        "rubric_dimensions": [],
        "evidences": {},
        "opinions": [],
        "final_report": None,
    }

    print(f"[auditor] Running graph for repo: {args.repo_url}")
    print(f"[auditor] PDF: {pdf_path}")

    result = graph.invoke(initial_state)

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

    # Save JSON output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    serialisable = {
        bucket: [ev.model_dump() for ev in items]
        for bucket, items in evidences.items()
    }
    output_path.write_text(json.dumps(serialisable, indent=2))
    print(f"\n[auditor] Evidence written to {output_path}")


if __name__ == "__main__":
    main()
