"""Tests for main.py CLI: checkpointing, thread_id, and output wiring.

No real graph execution or LLM calls — all external deps are mocked.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.state import Evidence


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestMainParser:
    def test_thread_id_arg_exists(self):
        from main import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            ["--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf"]
        )
        assert hasattr(args, "thread_id")
        assert args.thread_id is None

    def test_thread_id_arg_accepts_value(self):
        from main import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "--repo-url",
                "https://x.co/r",
                "--pdf-path",
                "/tmp/x.pdf",
                "--thread-id",
                "audit-20260227-120000-abc123",
            ]
        )
        assert args.thread_id == "audit-20260227-120000-abc123"

    def test_thread_id_help_mentions_resume(self):
        from main import _build_parser

        parser = _build_parser()
        help_text = parser.format_help()
        assert "thread" in help_text.lower()
        assert "resume" in help_text.lower() or "checkpoint" in help_text.lower()


# ---------------------------------------------------------------------------
# main() — checkpointing and thread_id wiring
# ---------------------------------------------------------------------------


def _fake_evidence_bundle():
    """Minimal evidence structure so main() can serialize and write."""
    return {
        "repo": [
            Evidence(
                goal="git_forensic_analysis",
                found=True,
                location="git log",
                rationale="Ok",
                confidence=0.9,
            )
        ],
        "doc": [],
        "vision": [],
    }


def _run_main_with_mocks(tmp_path, pdf_path, output_path, argv_extra=None):
    """Run main.main() with graph, SqliteSaver, and TRACKER mocked. Returns (mock_make_graph, mock_graph, written_json)."""
    argv = ["main", "--repo-url", "https://x.co/r", "--pdf-path", str(pdf_path), "--output", str(output_path)]
    if argv_extra:
        argv.extend(argv_extra)

    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {"evidences": _fake_evidence_bundle()}
    mock_make_graph = MagicMock(return_value=mock_graph)

    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = MagicMock()
    mock_cm.__exit__.return_value = None

    written_json = None

    with (
        patch("main._load_rubric", return_value=[]),
        patch("langgraph.checkpoint.sqlite.SqliteSaver") as SqliteSaverMock,
        patch("src.graph.make_graph", mock_make_graph),
        patch("src.utils.spend_tracker.TRACKER") as TrackerMock,
    ):
        SqliteSaverMock.from_conn_string.return_value = mock_cm
        TrackerMock.summary.return_value = {"total_cost_usd": 0.0}
        TrackerMock.report.return_value = ""

        import main

        with patch("sys.argv", argv):
            main.main()

        # Read what was written to the canonical output path
        if output_path.exists():
            written_json = json.loads(output_path.read_text())

    return mock_make_graph, mock_graph, written_json


class TestMainCheckpointing:
    """Test that main() uses SqliteSaver, make_graph(checkpointer), and thread_id."""

    def test_invoke_receives_config_with_thread_id_when_provided(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_text("x")
        out = tmp_path / "evidence.json"

        mock_make_graph, mock_graph, written = _run_main_with_mocks(
            tmp_path, pdf, out, ["--thread-id", "my-thread-42"]
        )

        call_kw = mock_graph.invoke.call_args
        assert call_kw is not None
        assert call_kw[1]["config"] == {"configurable": {"thread_id": "my-thread-42"}}
        assert written is not None
        assert written.get("thread_id") == "my-thread-42"

    def test_thread_id_in_written_json_when_provided(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_text("x")
        out = tmp_path / "evidence.json"

        _, _, written = _run_main_with_mocks(
            tmp_path, pdf, out, ["--thread-id", "resume-id-99"]
        )

        assert written is not None
        assert written.get("thread_id") == "resume-id-99"
        assert "evidences" in written
        assert "spend" in written

    def test_thread_id_generated_when_not_provided(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_text("x")
        out = tmp_path / "evidence.json"

        _, _, written = _run_main_with_mocks(tmp_path, pdf, out)

        assert written is not None
        tid = written.get("thread_id")
        assert tid is not None
        assert tid.startswith("audit-")
        # Format: audit-YYYYMMDD-HHMMSS-<6 hex>
        parts = tid.split("-")
        assert len(parts) >= 3
        assert len(parts[-1]) == 6

    def test_make_graph_called_with_checkpointer(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_text("x")
        out = tmp_path / "evidence.json"

        mock_make_graph, _, _ = _run_main_with_mocks(tmp_path, pdf, out)

        mock_make_graph.assert_called_once()
        call_kw = mock_make_graph.call_args[1]
        assert "checkpointer" in call_kw
        assert call_kw["checkpointer"] is not None

    def test_checkpoint_db_path_contains_checkpoints_db(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_text("x")
        out = tmp_path / "subdir" / "evidence.json"
        out.parent.mkdir(parents=True, exist_ok=True)

        with (
            patch("main._load_rubric", return_value=[]),
            patch("langgraph.checkpoint.sqlite.SqliteSaver") as SqliteSaverMock,
            patch("src.graph.make_graph", return_value=MagicMock(invoke=MagicMock(return_value={"evidences": _fake_evidence_bundle()}))),
            patch("src.utils.spend_tracker.TRACKER") as TrackerMock,
        ):
            mock_cm = MagicMock()
            mock_cm.__enter__.return_value = MagicMock()
            mock_cm.__exit__.return_value = None
            SqliteSaverMock.from_conn_string.return_value = mock_cm
            TrackerMock.summary.return_value = {}
            TrackerMock.report.return_value = ""

            import main

            with patch("sys.argv", ["main", "--repo-url", "https://x.co/r", "--pdf-path", str(pdf), "--output", str(out)]):
                main.main()

        call_args = SqliteSaverMock.from_conn_string.call_args
        assert call_args is not None
        conn_str = call_args[0][0]
        assert "checkpoints.db" in conn_str
