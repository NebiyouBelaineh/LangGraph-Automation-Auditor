"""Tests for src/nodes/detectives.py.

Each detective node is tested with mocked tool calls so no real
network, filesystem, or LLM is required.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.nodes.detectives import (
    doc_analyst_node,
    repo_investigator_node,
    vision_inspector_node,
)
from src.state import Evidence
from src.tools.doc_tools import Chunk, MatchedChunk, PdfIngestResult, PdfQueryResult
from src.tools.repo_tools import (
    CloneResult,
    GitHistoryResult,
    GraphAnalysisResult,
)
from src.tools.vision_tools import DiagramAnalysisResult, ExtractedImage


# ── fixtures ────────────────────────────────────────────────────────────────


def _base_state(**overrides):
    defaults = {
        "repo_url": "https://github.com/org/repo",
        "pdf_path": "/tmp/report.pdf",
        "rubric_dimensions": [],
        "evidences": {},
        "opinions": [],
        "final_report": None,
    }
    defaults.update(overrides)
    return defaults


# ── RepoInvestigatorNode ─────────────────────────────────────────────────────


class TestRepoInvestigatorNode:
    def test_emits_all_dimensions_on_success(self):
        clone_ok = CloneResult(ok=True, cloned_path="/tmp/repo")
        git_ok = GitHistoryResult(
            ok=True, commit_count=10, progression_detected=True,
            progression_summary="setup → tools → nodes",
        )
        graph_ok = GraphAnalysisResult(
            ok=True, nodes=["entry", "worker"], edges=[("entry", "worker")],
            has_parallel_branches=True, has_fan_in=True,
        )

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_ok),
            patch("src.nodes.detectives.extract_git_history", return_value=git_ok),
            patch("src.nodes.detectives.analyze_graph_structure", return_value=graph_ok),
            patch("pathlib.Path.exists", return_value=False),  # no state.py in tmp
        ):
            result = repo_investigator_node(_base_state())

        evidences = result["evidences"]["repo"]
        goals = {e.goal for e in evidences}
        assert "git_forensic_analysis" in goals
        assert "graph_orchestration" in goals
        assert "safe_tool_engineering" in goals

    def test_emits_found_false_on_clone_failure(self):
        clone_fail = CloneResult(ok=False, error="clone_failed", details="Not found")

        with patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_fail):
            result = repo_investigator_node(_base_state())

        evidences = result["evidences"]["repo"]
        assert all(not e.found for e in evidences)

    def test_git_history_failure_produces_found_false_evidence(self):
        clone_ok = CloneResult(ok=True, cloned_path="/tmp/repo")
        git_fail = GitHistoryResult(ok=False, error="git_log_failed")
        graph_ok = GraphAnalysisResult(ok=True, nodes=[], edges=[])

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_ok),
            patch("src.nodes.detectives.extract_git_history", return_value=git_fail),
            patch("src.nodes.detectives.analyze_graph_structure", return_value=graph_ok),
            patch("pathlib.Path.exists", return_value=False),
        ):
            result = repo_investigator_node(_base_state())

        git_ev = next(e for e in result["evidences"]["repo"] if e.goal == "git_forensic_analysis")
        assert git_ev.found is False

    def test_high_confidence_for_full_graph_structure(self):
        clone_ok = CloneResult(ok=True, cloned_path="/tmp/repo")
        git_ok = GitHistoryResult(ok=True, commit_count=5, progression_detected=True)
        graph_ok = GraphAnalysisResult(
            ok=True, nodes=["a", "b", "c"], edges=[],
            has_parallel_branches=True, has_fan_in=True,
        )

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_ok),
            patch("src.nodes.detectives.extract_git_history", return_value=git_ok),
            patch("src.nodes.detectives.analyze_graph_structure", return_value=graph_ok),
            patch("pathlib.Path.exists", return_value=False),
        ):
            result = repo_investigator_node(_base_state())

        graph_ev = next(
            e for e in result["evidences"]["repo"] if e.goal == "graph_orchestration"
        )
        assert graph_ev.confidence >= 0.8

    def test_safe_tool_engineering_always_emitted(self):
        clone_ok = CloneResult(ok=True, cloned_path="/tmp/repo")
        git_ok = GitHistoryResult(ok=True, commit_count=1)
        graph_ok = GraphAnalysisResult(ok=True, nodes=[], edges=[])

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_ok),
            patch("src.nodes.detectives.extract_git_history", return_value=git_ok),
            patch("src.nodes.detectives.analyze_graph_structure", return_value=graph_ok),
            patch("pathlib.Path.exists", return_value=False),
        ):
            result = repo_investigator_node(_base_state())

        goals = {e.goal for e in result["evidences"]["repo"]}
        assert "safe_tool_engineering" in goals


# ── DocAnalystNode ────────────────────────────────────────────────────────────


class TestDocAnalystNode:
    def _chunks(self):
        return [
            Chunk("c0", "Dialectical Synthesis in LangGraph fan-out fan-in.", 1, 1),
            Chunk("c1", "src/graph.py and src/nodes/detectives.py are key files.", 2, 2),
        ]

    def test_emits_theoretical_depth_and_report_accuracy(self):
        ingest_ok = PdfIngestResult(ok=True, chunks=self._chunks(), page_count=2)
        query_ok = PdfQueryResult(
            ok=True, query="Dialectical Synthesis",
            matches=[MatchedChunk("c0", "snippet", 0.5, (1, 1))],
        )

        with (
            patch("src.nodes.detectives.ingest_pdf", return_value=ingest_ok),
            patch("src.nodes.detectives.query_pdf", return_value=query_ok),
            patch("src.nodes.detectives.extract_file_paths_from_text", return_value=["src/graph.py"]),
        ):
            result = doc_analyst_node(_base_state())

        goals = {e.goal for e in result["evidences"]["doc"]}
        assert "theoretical_depth" in goals
        assert "report_accuracy" in goals

    def test_emits_found_false_on_ingest_failure(self):
        ingest_fail = PdfIngestResult(ok=False, error="file_not_found")

        with patch("src.nodes.detectives.ingest_pdf", return_value=ingest_fail):
            result = doc_analyst_node(_base_state())

        assert all(not e.found for e in result["evidences"]["doc"])

    def test_theoretical_depth_confidence_scales_with_matches(self):
        chunks = self._chunks()
        ingest_ok = PdfIngestResult(ok=True, chunks=chunks, page_count=2)

        # All rubric queries find matches
        query_hit = PdfQueryResult(
            ok=True, query="q",
            matches=[MatchedChunk("c0", "text", 0.8, (1, 1))],
        )

        with (
            patch("src.nodes.detectives.ingest_pdf", return_value=ingest_ok),
            patch("src.nodes.detectives.query_pdf", return_value=query_hit),
            patch("src.nodes.detectives.extract_file_paths_from_text", return_value=[]),
        ):
            result = doc_analyst_node(_base_state())

        depth_ev = next(e for e in result["evidences"]["doc"] if e.goal == "theoretical_depth")
        assert depth_ev.confidence > 0.5

    def test_no_exception_on_zero_file_paths(self):
        ingest_ok = PdfIngestResult(ok=True, chunks=self._chunks(), page_count=1)
        query_miss = PdfQueryResult(ok=True, query="q", matches=[])

        with (
            patch("src.nodes.detectives.ingest_pdf", return_value=ingest_ok),
            patch("src.nodes.detectives.query_pdf", return_value=query_miss),
            patch("src.nodes.detectives.extract_file_paths_from_text", return_value=[]),
        ):
            result = doc_analyst_node(_base_state())  # must not raise

        assert "doc" in result["evidences"]


# ── VisionInspectorNode ───────────────────────────────────────────────────────


class TestVisionInspectorNode:
    _fake_image = ExtractedImage(image_bytes=b"fakepng", page_number=1, image_index=0)

    def test_emits_found_false_when_no_images(self):
        with patch("src.nodes.detectives.extract_images_from_pdf", return_value=[]):
            result = vision_inspector_node(_base_state())

        ev = result["evidences"]["vision"][0]
        assert ev.found is False
        assert ev.goal == "swarm_visual"

    def test_emits_evidence_for_accurate_stategraph(self):
        diagram = DiagramAnalysisResult(
            classification="accurate_stategraph",
            description="Parallel fan-out/fan-in detected.",
            has_parallel_branches=True,
            confidence=0.9,
        )

        with (
            patch("src.nodes.detectives.extract_images_from_pdf", return_value=[self._fake_image]),
            patch("src.nodes.detectives.analyze_diagram", return_value=diagram),
        ):
            result = vision_inspector_node(_base_state())

        ev = result["evidences"]["vision"][0]
        assert ev.found is True
        assert ev.goal == "swarm_visual"
        assert ev.confidence == pytest.approx(0.9)

    def test_confidence_halved_for_non_accurate_classification(self):
        diagram = DiagramAnalysisResult(
            classification="linear_pipeline",
            description="Simple linear flow.",
            has_parallel_branches=False,
            confidence=0.8,
        )

        with (
            patch("src.nodes.detectives.extract_images_from_pdf", return_value=[self._fake_image]),
            patch("src.nodes.detectives.analyze_diagram", return_value=diagram),
        ):
            result = vision_inspector_node(_base_state())

        ev = result["evidences"]["vision"][0]
        assert ev.confidence == pytest.approx(0.4)  # halved

    def test_selects_highest_confidence_image(self):
        images = [
            ExtractedImage(b"img1", 1, 0),
            ExtractedImage(b"img2", 2, 0),
        ]
        low = DiagramAnalysisResult("generic_flowchart", "generic", False, 0.3)
        high = DiagramAnalysisResult("accurate_stategraph", "accurate", True, 0.95)

        call_count = 0

        def side_effect(image_bytes, **kwargs):
            nonlocal call_count
            call_count += 1
            return low if call_count == 1 else high

        with (
            patch("src.nodes.detectives.extract_images_from_pdf", return_value=images),
            patch("src.nodes.detectives.analyze_diagram", side_effect=side_effect),
        ):
            result = vision_inspector_node(_base_state())

        ev = result["evidences"]["vision"][0]
        # Best (highest confidence) result should win
        assert ev.confidence == pytest.approx(0.95)

    def test_emits_found_false_on_analysis_error(self):
        diagram = DiagramAnalysisResult(
            classification="other", description="", has_parallel_branches=False,
            confidence=0.0, error="no_api_key",
        )

        with (
            patch("src.nodes.detectives.extract_images_from_pdf", return_value=[self._fake_image]),
            patch("src.nodes.detectives.analyze_diagram", return_value=diagram),
        ):
            result = vision_inspector_node(_base_state())

        ev = result["evidences"]["vision"][0]
        assert ev.found is False
