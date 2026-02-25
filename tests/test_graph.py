"""Tests for src/graph.py.

Covers: graph compilation, conditional routing, evidence aggregation,
early-abort on invalid inputs, and end-to-end state flow.
Detective nodes are mocked to avoid real network / LLM calls.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.graph import (
    _REQUIRED_DIMENSIONS,
    _abort_node,
    _input_router,
    entry_node,
    evidence_aggregator_node,
    graph,
)
from src.state import AgentState, Evidence


# ── helpers ──────────────────────────────────────────────────────────────────


def _state(**overrides) -> AgentState:
    defaults: AgentState = {
        "repo_url": "https://github.com/org/repo",
        "pdf_path": "/tmp/report.pdf",
        "rubric_dimensions": [],
        "evidences": {},
        "opinions": [],
        "final_report": None,
    }
    defaults.update(overrides)
    return defaults


def _evidence(goal: str, found: bool = True) -> Evidence:
    return Evidence(
        goal=goal, found=found, location="test", rationale="test", confidence=0.5
    )


# ── Graph compilation ────────────────────────────────────────────────────────


class TestGraphCompilation:
    def test_graph_compiles(self):
        assert graph is not None

    def test_graph_has_expected_nodes(self):
        node_keys = set(graph.nodes.keys())
        for expected in ["entry", "repo_investigator", "doc_analyst",
                         "vision_inspector", "evidence_aggregator", "abort"]:
            assert expected in node_keys, f"Missing node: {expected}"

    def test_graph_has_start_node(self):
        assert "__start__" in graph.nodes


# ── entry_node ───────────────────────────────────────────────────────────────


class TestEntryNode:
    def test_strips_whitespace_from_inputs(self):
        state = _state(repo_url="  https://github.com/org/repo  ", pdf_path="  /tmp/x.pdf  ")
        result = entry_node(state)
        assert result["repo_url"] == "https://github.com/org/repo"
        assert result["pdf_path"] == "/tmp/x.pdf"

    def test_initialises_evidence_buckets(self):
        result = entry_node(_state(evidences={}))
        for bucket in ("repo", "doc", "vision"):
            assert bucket in result["evidences"]

    def test_preserves_existing_evidences(self):
        existing = {"repo": [_evidence("git_forensic_analysis")]}
        result = entry_node(_state(evidences=existing))
        assert result["evidences"]["repo"][0].goal == "git_forensic_analysis"


# ── _input_router ─────────────────────────────────────────────────────────────


class TestInputRouter:
    def test_routes_valid_https_url(self):
        state = _state(repo_url="https://github.com/org/repo", pdf_path="/tmp/x.pdf")
        assert _input_router(state) == "detectives"

    def test_routes_valid_git_at_url(self):
        state = _state(repo_url="git@github.com:org/repo.git", pdf_path="/tmp/x.pdf")
        assert _input_router(state) == "detectives"

    def test_aborts_on_missing_repo_url(self):
        state = _state(repo_url="", pdf_path="/tmp/x.pdf")
        assert _input_router(state) == "abort"

    def test_aborts_on_missing_pdf_path(self):
        state = _state(repo_url="https://github.com/org/repo", pdf_path="")
        assert _input_router(state) == "abort"

    def test_aborts_on_invalid_url_scheme(self):
        state = _state(repo_url="ftp://evil.com/repo", pdf_path="/tmp/x.pdf")
        assert _input_router(state) == "abort"

    def test_aborts_on_bare_domain(self):
        state = _state(repo_url="github.com/org/repo", pdf_path="/tmp/x.pdf")
        assert _input_router(state) == "abort"


# ── _abort_node ───────────────────────────────────────────────────────────────


class TestAbortNode:
    def test_emits_found_false_for_all_dimensions(self):
        result = _abort_node(_state())
        all_dimensions = [dim for dims in _REQUIRED_DIMENSIONS.values() for dim in dims]

        emitted = {
            e.goal
            for evs in result["evidences"].values()
            for e in evs
        }
        for dim in all_dimensions:
            assert dim in emitted, f"Dimension {dim!r} not emitted by abort_node"

    def test_all_evidence_found_false(self):
        result = _abort_node(_state())
        for evs in result["evidences"].values():
            for ev in evs:
                assert ev.found is False


# ── evidence_aggregator_node ──────────────────────────────────────────────────


class TestEvidenceAggregatorNode:
    def test_backfills_missing_dimensions(self):
        # Provide no evidence at all
        result = evidence_aggregator_node(_state(evidences={"repo": [], "doc": [], "vision": []}))

        for bucket, dims in _REQUIRED_DIMENSIONS.items():
            goals = {e.goal for e in result["evidences"][bucket]}
            for dim in dims:
                assert dim in goals, f"Missing backfilled dimension {dim!r} in bucket {bucket!r}"

    def test_backfilled_evidence_is_found_false(self):
        result = evidence_aggregator_node(_state(evidences={}))
        for evs in result["evidences"].values():
            for ev in evs:
                if ev.rationale.startswith("No evidence collected"):
                    assert ev.found is False

    def test_preserves_existing_evidence(self):
        existing_ev = _evidence("git_forensic_analysis", found=True)
        evidences = {"repo": [existing_ev], "doc": [], "vision": []}

        result = evidence_aggregator_node(_state(evidences=evidences))

        repo_evs = {e.goal: e for e in result["evidences"]["repo"]}
        assert repo_evs["git_forensic_analysis"].found is True

    def test_all_required_dimensions_present_after_aggregation(self):
        result = evidence_aggregator_node(_state(evidences={}))
        for bucket, dims in _REQUIRED_DIMENSIONS.items():
            for dim in dims:
                goals = {e.goal for e in result["evidences"].get(bucket, [])}
                assert dim in goals


# ── End-to-end graph invocation ───────────────────────────────────────────────


class TestGraphEndToEnd:
    def _mock_detective(self, bucket: str) -> dict:
        """Return a partial evidences dict mimicking a detective node output."""
        dims = _REQUIRED_DIMENSIONS[bucket]
        return {"evidences": {bucket: [_evidence(d) for d in dims]}}

    def test_aborts_early_on_invalid_inputs(self):
        result = graph.invoke(
            _state(repo_url="", pdf_path=""),
        )
        all_evidences = [
            ev
            for evs in result["evidences"].values()
            for ev in evs
        ]
        assert all(not ev.found for ev in all_evidences)

    def test_valid_inputs_pass_through_detectives(self):
        repo_out = self._mock_detective("repo")
        doc_out = self._mock_detective("doc")
        vision_out = self._mock_detective("vision")

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed") as m_clone,
            patch("src.nodes.detectives.extract_git_history") as m_git,
            patch("src.nodes.detectives.analyze_graph_structure") as m_graph,
            patch("src.nodes.detectives.ingest_pdf") as m_ingest,
            patch("src.nodes.detectives.query_pdf") as m_query,
            patch("src.nodes.detectives.extract_file_paths_from_text", return_value=[]),
            patch("src.nodes.detectives.extract_images_from_pdf", return_value=[]),
        ):
            from src.tools.repo_tools import CloneResult, GitHistoryResult, GraphAnalysisResult
            from src.tools.doc_tools import PdfIngestResult, PdfQueryResult

            m_clone.return_value = CloneResult(ok=True, cloned_path="/tmp/r")
            m_git.return_value = GitHistoryResult(ok=True, commit_count=5)
            m_graph.return_value = GraphAnalysisResult(ok=True, nodes=[], edges=[])
            m_ingest.return_value = PdfIngestResult(ok=False, error="file_not_found")

            result = graph.invoke(_state())

        # All required dimensions must be present after aggregation
        for bucket, dims in _REQUIRED_DIMENSIONS.items():
            goals = {e.goal for e in result["evidences"].get(bucket, [])}
            for dim in dims:
                assert dim in goals, f"Missing {dim!r} in {bucket!r} after full run"

    def test_result_state_contains_evidences_key(self):
        with patch("src.nodes.detectives.clone_repo_sandboxed") as m:
            from src.tools.repo_tools import CloneResult
            m.return_value = CloneResult(ok=False, error="clone_failed")
            patch("src.nodes.detectives.ingest_pdf").start()
            patch("src.nodes.detectives.extract_images_from_pdf", return_value=[]).start()
            result = graph.invoke(_state())

        assert "evidences" in result
