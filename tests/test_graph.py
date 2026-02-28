"""Tests for src/graph.py.

Covers: graph compilation, conditional routing, evidence aggregation,
early-abort on invalid inputs, skip_detectives mode, judicial layer wiring,
and end-to-end state flow.
Detective nodes are no-ops via skip_detectives; judicial LLMs are mocked.
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
from src.state import AgentState, AuditReport, CriterionResult, Evidence, JudicialOpinion


# ── helpers ──────────────────────────────────────────────────────────────────


def _state(**overrides) -> AgentState:
    defaults: AgentState = {
        "repo_url": "https://github.com/org/repo",
        "pdf_path": "/tmp/report.pdf",
        "rubric_dimensions": [],
        "clone_options": {},
        "skip_detectives": False,
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


def _fake_opinion(dim_id: str = "git_forensic_analysis", judge: str = "Prosecutor") -> JudicialOpinion:
    return JudicialOpinion(
        judge=judge,  # type: ignore[arg-type]
        criterion_id=dim_id,
        score=3,
        argument="test argument",
        cited_evidence=["test_file.py"],
    )


def _rubric_dim(dim_id: str, artifact: str = "github_repo") -> dict:
    return {
        "id": dim_id,
        "name": dim_id.replace("_", " ").title(),
        "target_artifact": artifact,
        "success_pattern": "found",
        "failure_pattern": "not found",
        "forensic_instruction": "check it",
    }


def _mock_judge_llm(dim_ids: list[str], judge: str = "Prosecutor") -> MagicMock:
    """Return a ChatAnthropic mock whose structured-output invoke always returns a fake opinion."""
    opinions = [_fake_opinion(d, judge) for d in dim_ids]
    structured = MagicMock()
    structured.invoke.side_effect = opinions + [_fake_opinion(dim_ids[-1], judge)] * 100
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = structured
    return mock_llm


# ── Graph compilation ────────────────────────────────────────────────────────


class TestGraphCompilation:
    def test_graph_compiles(self):
        assert graph is not None

    def test_graph_has_expected_nodes(self):
        node_keys = set(graph.nodes.keys())
        expected_nodes = [
            # detective layer
            "entry", "repo_investigator", "doc_analyst",
            "vision_inspector", "evidence_aggregator", "abort",
            # judicial layer
            "prosecutor", "defense", "tech_lead",
            # chief justice
            "chief_justice",
        ]
        for expected in expected_nodes:
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

    def test_skip_detectives_bypasses_url_validation(self):
        """skip_detectives=True routes to 'detectives' even when repo_url is empty."""
        state = _state(repo_url="", pdf_path="", skip_detectives=True)
        assert _input_router(state) == "detectives"

    def test_skip_detectives_bypasses_invalid_url(self):
        state = _state(repo_url="ftp://bad", pdf_path="", skip_detectives=True)
        assert _input_router(state) == "detectives"


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
    """End-to-end graph tests.

    Detective nodes are bypassed via skip_detectives=True (no real cloning or
    PDF parsing).  Judicial LLMs are mocked via ChatAnthropic patch so no real
    API calls are made.
    """

    def _state_with_evidence(self, dims: list[dict]) -> AgentState:
        """Build a state with pre-loaded evidence for each dimension."""
        by_bucket: dict[str, list[Evidence]] = {"repo": [], "doc": [], "vision": []}
        bucket_map = {"github_repo": "repo", "pdf_report": "doc", "pdf_images": "vision"}
        for d in dims:
            bucket = bucket_map.get(d["target_artifact"], "repo")
            by_bucket[bucket].append(_evidence(d["id"]))
        return _state(
            skip_detectives=True,
            rubric_dimensions=dims,
            evidences=by_bucket,
        )

    def _patch_judges(self, dim_ids: list[str]):
        """Return a context manager that patches ChatAnthropic for all judge nodes."""
        # Each judge call gets its own opinion; cycle through dim_ids
        opinions_cycle = [_fake_opinion(d, "Prosecutor") for d in dim_ids] * 10
        structured = MagicMock()
        structured.invoke.side_effect = opinions_cycle
        mock_llm_inst = MagicMock()
        mock_llm_inst.with_structured_output.return_value = structured
        return patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm_inst)

    # ── abort path ───────────────────────────────────────────────────────────

    def test_aborts_early_on_invalid_inputs(self):
        """Invalid inputs (empty url/path, skip_detectives=False) trigger abort."""
        result = graph.invoke(_state(repo_url="", pdf_path=""))
        all_evidences = [ev for evs in result["evidences"].values() for ev in evs]
        assert all(not ev.found for ev in all_evidences)

    def test_result_state_always_contains_evidences_key(self):
        """Even on abort the result dict has an 'evidences' key."""
        result = graph.invoke(_state(repo_url="", pdf_path=""))
        assert "evidences" in result

    # ── skip_detectives path ─────────────────────────────────────────────────

    def test_skip_detectives_preserves_preloaded_evidence(self):
        """When skip_detectives=True, pre-loaded evidences reach evidence_aggregator intact."""
        dims = [_rubric_dim("git_forensic_analysis")]
        state = self._state_with_evidence(dims)
        with self._patch_judges(["git_forensic_analysis"]):
            result = graph.invoke(state)
        goals = {e.goal for evs in result["evidences"].values() for e in evs}
        assert "git_forensic_analysis" in goals

    def test_skip_detectives_produces_final_report(self):
        """Full pipeline with skip_detectives=True ends with a non-None final_report."""
        dims = [_rubric_dim("git_forensic_analysis")]
        state = self._state_with_evidence(dims)
        with self._patch_judges(["git_forensic_analysis"]):
            result = graph.invoke(state)
        assert result.get("final_report") is not None

    def test_skip_detectives_final_report_has_correct_score_range(self):
        """chief_justice produces a score in [1, 5]."""
        dims = [_rubric_dim("git_forensic_analysis")]
        state = self._state_with_evidence(dims)
        with self._patch_judges(["git_forensic_analysis"]):
            result = graph.invoke(state)
        report = result.get("final_report")
        assert report is not None
        assert 1.0 <= report.overall_score <= 5.0

    def test_skip_detectives_all_required_dimensions_backfilled(self):
        """evidence_aggregator backfills dimensions missing from pre-loaded evidence."""
        # Only provide one dimension in evidence; aggregator should backfill the rest
        dims = [_rubric_dim(d) for d in _REQUIRED_DIMENSIONS["repo"]]
        state = _state(
            skip_detectives=True,
            rubric_dimensions=dims,
            evidences={"repo": [_evidence("git_forensic_analysis")], "doc": [], "vision": []},
        )
        with self._patch_judges([d["id"] for d in dims]):
            result = graph.invoke(state)
        goals = {e.goal for e in result["evidences"].get("repo", [])}
        for dim in _REQUIRED_DIMENSIONS["repo"]:
            assert dim in goals, f"Dimension {dim!r} not backfilled"

    # ── full detective → judicial path (mocked detectives) ───────────────────

    def test_valid_inputs_pass_through_detectives_to_final_report(self):
        """With mocked clone + judicial LLMs, a full run produces final_report."""
        from src.tools.repo_tools import CloneResult

        dims = [_rubric_dim("git_forensic_analysis")]
        with (
            patch("src.nodes.detectives.clone_repo_sandboxed",
                  return_value=CloneResult(ok=False, error="clone_failed")),
            patch("src.nodes.detectives.ChatAnthropic") as mock_det_llm_cls,
            self._patch_judges(["git_forensic_analysis"]),
        ):
            # Detective LLM returns an Evidence-structured output
            det_ev = _evidence("git_forensic_analysis")
            mock_det_structured = MagicMock()
            mock_det_structured.invoke.return_value = det_ev
            mock_det_llm_inst = MagicMock()
            mock_det_llm_inst.with_structured_output.return_value = mock_det_structured
            mock_det_llm_cls.return_value = mock_det_llm_inst

            result = graph.invoke(_state(rubric_dimensions=dims))

        assert "evidences" in result
        assert result.get("final_report") is not None
