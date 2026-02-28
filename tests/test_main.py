"""Tests for main.py CLI: checkpointing, thread_id, output wiring,
--evidence-path mode, --verbose/--quiet, and --branch/--depth.

No real graph execution or LLM calls — all external deps are mocked.
"""

import json
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Parser — new flags
# ---------------------------------------------------------------------------


class TestParserNewFlags:
    def _parse(self, *args):
        from main import _build_parser
        return _build_parser().parse_args(list(args))

    # ── --evidence-path ──────────────────────────────────────────────────────

    def test_evidence_path_arg_exists(self):
        args = self._parse("--evidence-path", "/tmp/ev.json")
        assert args.evidence_path == "/tmp/ev.json"

    def test_evidence_path_defaults_to_none(self):
        args = self._parse("--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf")
        assert args.evidence_path is None

    # ── --verbose / --quiet ──────────────────────────────────────────────────

    def test_verbose_is_default(self):
        args = self._parse("--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf")
        assert args.verbose is True

    def test_quiet_sets_verbose_false(self):
        args = self._parse(
            "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf", "--quiet"
        )
        assert args.verbose is False

    def test_verbose_flag_sets_verbose_true(self):
        args = self._parse(
            "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf", "--verbose"
        )
        assert args.verbose is True

    def test_verbose_and_quiet_are_mutually_exclusive(self):
        from main import _build_parser
        import argparse
        with pytest.raises(SystemExit):
            _build_parser().parse_args([
                "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf",
                "--verbose", "--quiet",
            ])

    # ── --branch / --depth ───────────────────────────────────────────────────

    def test_branch_defaults_to_none(self):
        args = self._parse("--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf")
        assert args.branch is None

    def test_branch_flag_sets_value(self):
        args = self._parse(
            "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf",
            "--branch", "develop",
        )
        assert args.branch == "develop"

    def test_branch_short_flag(self):
        args = self._parse(
            "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf",
            "-b", "main",
        )
        assert args.branch == "main"

    def test_depth_defaults_to_50(self):
        args = self._parse("--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf")
        assert args.depth == 50

    def test_depth_zero_accepted(self):
        args = self._parse(
            "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf",
            "--depth", "0",
        )
        assert args.depth == 0

    def test_depth_custom_value(self):
        args = self._parse(
            "--repo-url", "https://x.co/r", "--pdf-path", "/tmp/x.pdf",
            "--depth", "200",
        )
        assert args.depth == 200


# ---------------------------------------------------------------------------
# _load_evidence helper
# ---------------------------------------------------------------------------


class TestLoadEvidence:
    def test_exits_on_missing_file(self, tmp_path):
        from main import _load_evidence
        with pytest.raises(SystemExit):
            _load_evidence(tmp_path / "nonexistent.json")

    def test_exits_on_invalid_json(self, tmp_path):
        from main import _load_evidence
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{{")
        with pytest.raises(SystemExit):
            _load_evidence(bad)

    def test_exits_on_missing_evidences_key(self, tmp_path):
        from main import _load_evidence
        f = tmp_path / "ev.json"
        f.write_text(json.dumps({"thread_id": "x"}))
        with pytest.raises(SystemExit):
            _load_evidence(f)

    def test_returns_evidence_objects(self, tmp_path):
        from main import _load_evidence
        payload = {
            "thread_id": "test",
            "evidences": {
                "repo": [
                    {
                        "goal": "git_forensic_analysis",
                        "found": True,
                        "content": None,
                        "location": "git log",
                        "rationale": "good",
                        "confidence": 0.9,
                    }
                ],
                "doc": [],
                "vision": [],
            },
        }
        f = tmp_path / "ev.json"
        f.write_text(json.dumps(payload))
        result = _load_evidence(f)
        assert "repo" in result
        assert len(result["repo"]) == 1
        assert result["repo"][0].goal == "git_forensic_analysis"
        assert result["repo"][0].found is True

    def test_returns_correct_bucket_count(self, tmp_path):
        from main import _load_evidence
        payload = {
            "evidences": {
                "repo": [{"goal": "g1", "found": False, "content": None,
                          "location": "x", "rationale": "r", "confidence": 0.0}],
                "doc": [],
            }
        }
        f = tmp_path / "ev.json"
        f.write_text(json.dumps(payload))
        result = _load_evidence(f)
        assert set(result.keys()) == {"repo", "doc"}


# ---------------------------------------------------------------------------
# main() — --evidence-path mode
# ---------------------------------------------------------------------------


def _make_evidence_file(tmp_path: Path) -> Path:
    """Write a minimal evidence JSON and return its path."""
    payload = {
        "thread_id": "audit-test",
        "evidences": {
            "repo": [
                {
                    "goal": "git_forensic_analysis",
                    "found": True,
                    "content": None,
                    "location": "git log",
                    "rationale": "ok",
                    "confidence": 0.95,
                }
            ],
            "doc": [],
            "vision": [],
        },
    }
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(payload))
    return p


class TestEvidencePathMode:
    def test_evidence_path_mode_does_not_require_repo_url(self, tmp_path):
        ev_file = _make_evidence_file(tmp_path)
        out = tmp_path / "output.json"

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"evidences": {}, "final_report": None}
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = MagicMock()
        mock_cm.__exit__.return_value = None

        with (
            patch("main._load_rubric", return_value=[]),
            patch("langgraph.checkpoint.sqlite.SqliteSaver") as SqliteMock,
            patch("src.graph.make_graph", return_value=mock_graph),
            patch("src.utils.spend_tracker.TRACKER") as TrackerMock,
        ):
            SqliteMock.from_conn_string.return_value = mock_cm
            TrackerMock.summary.return_value = {}
            TrackerMock.report.return_value = ""

            import main

            with patch("sys.argv", [
                "main", "--evidence-path", str(ev_file), "--output", str(out)
            ]):
                main.main()  # should not raise SystemExit

    def test_evidence_path_sets_skip_detectives_in_state(self, tmp_path):
        ev_file = _make_evidence_file(tmp_path)
        out = tmp_path / "output.json"

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"evidences": {}, "final_report": None}
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = MagicMock()
        mock_cm.__exit__.return_value = None

        with (
            patch("main._load_rubric", return_value=[]),
            patch("langgraph.checkpoint.sqlite.SqliteSaver") as SqliteMock,
            patch("src.graph.make_graph", return_value=mock_graph),
            patch("src.utils.spend_tracker.TRACKER") as TrackerMock,
        ):
            SqliteMock.from_conn_string.return_value = mock_cm
            TrackerMock.summary.return_value = {}
            TrackerMock.report.return_value = ""

            import main

            with patch("sys.argv", [
                "main", "--evidence-path", str(ev_file), "--output", str(out)
            ]):
                main.main()

        invoked_state = mock_graph.invoke.call_args[0][0]
        assert invoked_state.get("skip_detectives") is True

    def test_evidence_path_preloads_evidences_into_state(self, tmp_path):
        ev_file = _make_evidence_file(tmp_path)
        out = tmp_path / "output.json"

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"evidences": {}, "final_report": None}
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = MagicMock()
        mock_cm.__exit__.return_value = None

        with (
            patch("main._load_rubric", return_value=[]),
            patch("langgraph.checkpoint.sqlite.SqliteSaver") as SqliteMock,
            patch("src.graph.make_graph", return_value=mock_graph),
            patch("src.utils.spend_tracker.TRACKER") as TrackerMock,
        ):
            SqliteMock.from_conn_string.return_value = mock_cm
            TrackerMock.summary.return_value = {}
            TrackerMock.report.return_value = ""

            import main

            with patch("sys.argv", [
                "main", "--evidence-path", str(ev_file), "--output", str(out)
            ]):
                main.main()

        invoked_state = mock_graph.invoke.call_args[0][0]
        evidences = invoked_state.get("evidences", {})
        assert "repo" in evidences
        assert any(e.goal == "git_forensic_analysis" for e in evidences["repo"])
