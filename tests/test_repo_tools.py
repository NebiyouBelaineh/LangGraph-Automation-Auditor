"""Tests for src/tools/repo_tools.py.

Covers: clone_repo_sandboxed, extract_git_history, analyze_graph_structure.
Subprocess calls are mocked; no real network or git binary required.
"""

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.tools.repo_tools import (
    CloneResult,
    GitHistoryResult,
    GraphAnalysisResult,
    _classify_clone_error,
    _sanitize_url,
    analyze_graph_structure,
    cleanup_clone_dir,
    clone_repo_sandboxed,
    extract_git_history,
)


# ---------------------------------------------------------------------------
# Helpers: shared TemporaryDirectory mock factory
# ---------------------------------------------------------------------------


def _make_td_mock(name: str = "/tmp/auditor_test") -> MagicMock:
    """Return a mock that behaves like tempfile.TemporaryDirectory."""
    td = MagicMock()
    td.name = name
    return td


# ── URL sanitization ────────────────────────────────────────────────────────


class TestSanitizeUrl:
    def test_strips_leading_trailing_whitespace(self):
        url, err = _sanitize_url("  https://github.com/org/repo  ")
        assert url == "https://github.com/org/repo"
        assert err is None

    def test_rejects_embedded_newline(self):
        _, err = _sanitize_url("https://github.com/org/repo\nextra")
        assert err is not None

    def test_rejects_carriage_return(self):
        _, err = _sanitize_url("https://github.com/org/repo\r")
        assert err is not None

    def test_rejects_null_byte(self):
        _, err = _sanitize_url("https://github.com/org/repo\x00")
        assert err is not None

    def test_accepts_clean_https_url(self):
        url, err = _sanitize_url("https://github.com/org/repo")
        assert url == "https://github.com/org/repo"
        assert err is None

    def test_accepts_clean_git_at_url(self):
        url, err = _sanitize_url("git@github.com:org/repo.git")
        assert url == "git@github.com:org/repo.git"
        assert err is None


# ── Auth error classification ───────────────────────────────────────────────


class TestClassifyCloneError:
    def test_authentication_failed_returns_auth_failed(self):
        code, msg = _classify_clone_error("fatal: Authentication failed for 'https://github.com/org/repo'")
        assert code == "auth_failed"
        assert "credential" in msg.lower() or "authentication" in msg.lower()

    def test_permission_denied_returns_auth_failed(self):
        code, msg = _classify_clone_error("Permission denied (publickey).")
        assert code == "auth_failed"

    def test_could_not_read_username_returns_auth_failed(self):
        code, msg = _classify_clone_error("fatal: could not read Username for 'https://github.com'")
        assert code == "auth_failed"

    def test_repository_not_found_returns_repo_not_found(self):
        code, msg = _classify_clone_error("ERROR: Repository not found.")
        assert code == "repo_not_found"

    def test_network_error_returns_network_error(self):
        code, msg = _classify_clone_error("fatal: unable to connect, could not resolve host: github.com")
        assert code == "network_error"

    def test_timed_out_returns_timeout_error(self):
        code, msg = _classify_clone_error("fatal: timed out waiting for remote")
        assert code == "timeout_error"

    def test_unknown_error_returns_clone_failed(self):
        code, msg = _classify_clone_error("fatal: some unknown git error")
        assert code == "clone_failed"
        assert "some unknown git error" in msg


# ── clone_repo_sandboxed ────────────────────────────────────────────────────


class TestCloneRepoSandboxed:
    def test_rejects_non_https_url(self):
        result = clone_repo_sandboxed("ftp://evil.com/repo")
        assert result.ok is False
        assert result.error == "invalid_url"
        assert result.user_message is not None

    def test_rejects_bare_domain(self):
        result = clone_repo_sandboxed("github.com/org/repo")
        assert result.ok is False
        assert result.error == "invalid_url"

    def test_rejects_url_with_embedded_newline(self):
        result = clone_repo_sandboxed("https://github.com/org/repo\nextra")
        assert result.ok is False
        assert result.error == "invalid_url"

    def test_rejects_url_with_null_byte(self):
        result = clone_repo_sandboxed("https://github.com/org/repo\x00")
        assert result.ok is False
        assert result.error == "invalid_url"

    def test_accepts_https_url(self, mocker):
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_td = _make_td_mock("/tmp/auditor_test")
        mocker.patch("src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=mock_td)

        result = clone_repo_sandboxed("https://github.com/org/repo")

        assert result.ok is True
        assert result.cloned_path == "/tmp/auditor_test"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "git" in cmd and "clone" in cmd
        assert "shell" not in str(mock_run.call_args)  # no shell=True

    def test_accepts_git_at_url(self, mocker):
        mocker.patch("src.tools.repo_tools.subprocess.run").return_value = MagicMock(
            returncode=0, stderr=""
        )
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )
        result = clone_repo_sandboxed("git@github.com:org/repo.git")
        assert result.ok is True

    def test_uses_temporary_directory_not_cwd(self, mocker):
        """Verify clone uses TemporaryDirectory (not mkdtemp) and never uses cwd as target."""
        mock_td_cls = mocker.patch("src.tools.repo_tools.tempfile.TemporaryDirectory")
        mock_td_cls.return_value = _make_td_mock("/tmp/safe")
        mocker.patch("src.tools.repo_tools.subprocess.run").return_value = MagicMock(
            returncode=0, stderr=""
        )
        clone_repo_sandboxed("https://github.com/org/repo")
        mock_td_cls.assert_called_once()

    def test_returns_classified_error_on_auth_failure(self, mocker):
        mocker.patch("src.tools.repo_tools.subprocess.run").return_value = MagicMock(
            returncode=128, stderr="fatal: Authentication failed for 'https://github.com/org/repo'"
        )
        mock_td = _make_td_mock("/tmp/x")
        mocker.patch("src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=mock_td)

        result = clone_repo_sandboxed("https://github.com/org/nonexistent")

        assert result.ok is False
        assert result.error == "auth_failed"
        assert result.user_message is not None
        assert "authentication" in result.user_message.lower() or "credential" in result.user_message.lower()

    def test_returns_classified_error_on_clone_failure(self, mocker):
        mocker.patch("src.tools.repo_tools.subprocess.run").return_value = MagicMock(
            returncode=128, stderr="ERROR: Repository not found."
        )
        mock_td = _make_td_mock("/tmp/x")
        mocker.patch("src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=mock_td)

        result = clone_repo_sandboxed("https://github.com/org/nonexistent")

        assert result.ok is False
        assert result.error == "repo_not_found"
        assert "Repository not found" in (result.details or "")

    def test_default_depth_50_in_command(self, mocker):
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )

        clone_repo_sandboxed("https://github.com/org/repo")

        cmd = mock_run.call_args[0][0]
        assert "--depth" in cmd
        assert "50" in cmd

    def test_branch_option_added_to_command(self, mocker):
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )

        clone_repo_sandboxed("https://github.com/org/repo", branch="develop")

        cmd = mock_run.call_args[0][0]
        assert "--branch" in cmd
        assert "develop" in cmd

    def test_depth_zero_omits_depth_flag(self, mocker):
        """depth=0 means a full (non-shallow) clone — --depth must not appear."""
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )

        clone_repo_sandboxed("https://github.com/org/repo", depth=0)

        cmd = mock_run.call_args[0][0]
        assert "--depth" not in cmd

    def test_custom_depth_in_command(self, mocker):
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )

        clone_repo_sandboxed("https://github.com/org/repo", depth=200)

        cmd = mock_run.call_args[0][0]
        assert "--depth" in cmd
        assert "200" in cmd

    def test_branch_and_depth_combined(self, mocker):
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )

        clone_repo_sandboxed("https://github.com/org/repo", branch="v1.0.0", depth=1)

        cmd = mock_run.call_args[0][0]
        assert "--branch" in cmd and "v1.0.0" in cmd
        assert "--depth" in cmd and "1" in cmd

    def test_no_branch_when_not_specified(self, mocker):
        mock_run = mocker.patch("src.tools.repo_tools.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mocker.patch(
            "src.tools.repo_tools.tempfile.TemporaryDirectory", return_value=_make_td_mock("/tmp/x")
        )

        clone_repo_sandboxed("https://github.com/org/repo")

        cmd = mock_run.call_args[0][0]
        assert "--branch" not in cmd


# ── cleanup_clone_dir ───────────────────────────────────────────────────────


class TestCleanupCloneDir:
    def test_returns_false_for_unknown_path(self, tmp_path):
        result = cleanup_clone_dir(str(tmp_path / "does_not_exist"))
        assert result is False

    def test_removes_existing_directory(self, tmp_path):
        d = tmp_path / "clone"
        d.mkdir()
        result = cleanup_clone_dir(str(d))
        assert result is True
        assert not d.exists()


# ── extract_git_history ─────────────────────────────────────────────────────


class TestExtractGitHistory:
    def test_detects_not_a_git_repo(self, tmp_path):
        result = extract_git_history(tmp_path)
        assert result.ok is False
        assert result.error == "not_a_git_repo"

    def test_parses_commits(self, tmp_path):
        (tmp_path / ".git").mkdir()
        git_log_output = (
            "abc1234\t2025-01-01 10:00:00 +0000\tAlice\tinit: scaffold project\n"
            "def5678\t2025-01-02 10:00:00 +0000\tAlice\tadd: repo_tools clone utility\n"
            "ghi9012\t2025-01-03 10:00:00 +0000\tBob\tadd: detective nodes\n"
        )
        with patch("src.tools.repo_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=git_log_output)
            result = extract_git_history(tmp_path)

        assert result.ok is True
        assert result.commit_count == 3
        assert result.commits[0].hash == "abc1234"
        assert result.commits[0].author == "Alice"
        assert result.commits[2].message == "add: detective nodes"

    def test_detects_progression(self, tmp_path):
        (tmp_path / ".git").mkdir()
        git_log_output = (
            "a\t2025-01-01\tDev\tinit: setup pyproject.toml\n"
            "b\t2025-01-02\tDev\tadd: repo_tools clone\n"
            "c\t2025-01-03\tDev\tadd: detective nodes graph\n"
        )
        with patch("src.tools.repo_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=git_log_output)
            result = extract_git_history(tmp_path)

        assert result.progression_detected is True

    def test_handles_empty_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("src.tools.repo_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = extract_git_history(tmp_path)

        assert result.ok is True
        assert result.commit_count == 0
        assert result.commits == []

    def test_handles_git_failure(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("src.tools.repo_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fatal: bad object")
            result = extract_git_history(tmp_path)

        assert result.ok is False
        assert "git_log_failed" in result.error


# ── analyze_graph_structure ─────────────────────────────────────────────────


class TestAnalyzeGraphStructure:
    def _write_graph(self, tmp_path: Path, source: str) -> Path:
        src_dir = tmp_path / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        graph_file = src_dir / "graph.py"
        graph_file.write_text(textwrap.dedent(source))
        return tmp_path

    def test_returns_error_when_no_file(self, tmp_path):
        result = analyze_graph_structure(tmp_path)
        assert result.ok is False
        assert result.error == "file_not_found"

    def test_detects_stategraph_nodes_and_edges(self, tmp_path):
        source = """
            from langgraph.graph import StateGraph, START, END

            builder = StateGraph(AgentState)
            builder.add_node("entry", entry_fn)
            builder.add_node("worker", worker_fn)
            builder.add_edge(START, "entry")
            builder.add_edge("entry", "worker")
            builder.add_edge("worker", END)
            graph = builder.compile()
        """
        repo = self._write_graph(tmp_path, source)
        result = analyze_graph_structure(repo)

        assert result.ok is True
        assert "entry" in result.nodes
        assert "worker" in result.nodes
        assert ("entry", "worker") in result.edges

    def test_detects_fan_out(self, tmp_path):
        source = """
            from langgraph.graph import StateGraph, START, END

            builder = StateGraph(State)
            builder.add_node("entry", fn)
            builder.add_node("a", fn_a)
            builder.add_node("b", fn_b)
            builder.add_node("c", fn_c)
            builder.add_edge("entry", "a")
            builder.add_edge("entry", "b")
            builder.add_edge("entry", "c")
            graph = builder.compile()
        """
        repo = self._write_graph(tmp_path, source)
        result = analyze_graph_structure(repo)

        assert result.ok is True
        assert result.has_parallel_branches is True

    def test_detects_fan_in(self, tmp_path):
        source = """
            from langgraph.graph import StateGraph, START, END

            builder = StateGraph(State)
            builder.add_node("a", fn_a)
            builder.add_node("b", fn_b)
            builder.add_node("agg", fn_agg)
            builder.add_edge("a", "agg")
            builder.add_edge("b", "agg")
            graph = builder.compile()
        """
        repo = self._write_graph(tmp_path, source)
        result = analyze_graph_structure(repo)

        assert result.ok is True
        assert result.has_fan_in is True

    def test_counts_conditional_edges(self, tmp_path):
        source = """
            from langgraph.graph import StateGraph, START, END

            builder = StateGraph(State)
            builder.add_node("router", fn)
            builder.add_conditional_edges("router", my_router, {"a": "node_a", "b": "node_b"})
            graph = builder.compile()
        """
        repo = self._write_graph(tmp_path, source)
        result = analyze_graph_structure(repo)

        assert result.ok is True
        assert result.conditional_edges_count == 1

    def test_handles_syntax_error(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "graph.py").write_text("StateGraph(\n\ndef broken {{{")
        result = analyze_graph_structure(tmp_path)

        assert result.ok is False
        assert "parse_error" in result.error

    def test_no_stategraph_found(self, tmp_path):
        source = "x = 1\ny = 2\n"
        repo = self._write_graph(tmp_path, source)
        result = analyze_graph_structure(repo)

        assert result.ok is False
        assert result.error == "no_stategraph_found"
