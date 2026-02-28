"""Tests for src/nodes/judges.py.

Covers: TRACKER integration (set_node + callback passing), _invoke_judge
retry/fallback logic, and the three public node functions.

All LLM calls are mocked — no real API calls are made.
"""

from unittest.mock import MagicMock, call, patch

import pytest
from pydantic import ValidationError

from src.state import AgentState, Evidence, JudicialOpinion


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


def _dim(dim_id: str = "git_forensic_analysis") -> dict:
    return {
        "id": dim_id,
        "name": dim_id.replace("_", " ").title(),
        "target_artifact": "github_repo",
        "success_pattern": "found",
        "failure_pattern": "not found",
        "forensic_instruction": "check git",
    }


def _opinion(dim_id: str = "git_forensic_analysis", judge: str = "Prosecutor") -> JudicialOpinion:
    return JudicialOpinion(
        judge=judge,  # type: ignore[arg-type]
        criterion_id=dim_id,
        score=3,
        argument="test argument",
        cited_evidence=["src/test.py"],
    )


def _mock_llm_cls(return_value: JudicialOpinion | None = None, side_effect=None):
    """Return a patched ChatAnthropic class whose structured invoke returns return_value."""
    structured = MagicMock()
    if side_effect:
        structured.invoke.side_effect = side_effect
    else:
        structured.invoke.return_value = return_value or _opinion()
    inst = MagicMock()
    inst.with_structured_output.return_value = structured
    return inst


# ── TRACKER wiring ────────────────────────────────────────────────────────────


class TestTrackerIntegration:
    """Verify that judge nodes call TRACKER.set_node and pass TRACKER as a callback."""

    def _run_prosecutor(self, state, mock_llm_inst):
        from src.nodes.judges import prosecutor_node
        with patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm_inst):
            with patch("src.nodes.judges.TRACKER") as mock_tracker:
                result = prosecutor_node(state)
        return result, mock_tracker

    def test_set_node_called_with_prosecutor_label(self):
        dims = [_dim()]
        state = _state(rubric_dimensions=dims)
        _, mock_tracker = self._run_prosecutor(state, _mock_llm_cls())
        mock_tracker.set_node.assert_called_once_with("judge:prosecutor")

    def test_set_node_called_with_defense_label(self):
        from src.nodes.judges import defense_node
        dims = [_dim()]
        state = _state(rubric_dimensions=dims)
        with patch("src.nodes.judges.ChatAnthropic", return_value=_mock_llm_cls()):
            with patch("src.nodes.judges.TRACKER") as mock_tracker:
                defense_node(state)
        mock_tracker.set_node.assert_called_once_with("judge:defense")

    def test_set_node_called_with_tech_lead_label(self):
        from src.nodes.judges import tech_lead_node
        dims = [_dim()]
        state = _state(rubric_dimensions=dims)
        with patch("src.nodes.judges.ChatAnthropic", return_value=_mock_llm_cls()):
            with patch("src.nodes.judges.TRACKER") as mock_tracker:
                tech_lead_node(state)
        mock_tracker.set_node.assert_called_once_with("judge:techlead")

    def test_invoke_called_with_tracker_callback(self):
        """judge_llm.invoke must be called with config containing TRACKER."""
        from src.nodes.judges import prosecutor_node
        from src.utils.spend_tracker import TRACKER as real_tracker

        dims = [_dim()]
        state = _state(rubric_dimensions=dims)

        structured = MagicMock()
        structured.invoke.return_value = _opinion()
        mock_llm_inst = MagicMock()
        mock_llm_inst.with_structured_output.return_value = structured

        with patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm_inst):
            prosecutor_node(state)

        # invoke must have been called with a config kwarg containing callbacks
        call_kwargs = structured.invoke.call_args
        assert call_kwargs is not None
        config = call_kwargs[1].get("config", {})
        callbacks = config.get("callbacks", [])
        assert any(cb is real_tracker for cb in callbacks), (
            "TRACKER must be in the callbacks passed to judge_llm.invoke"
        )


# ── _invoke_judge retry / fallback ────────────────────────────────────────────


class TestInvokeJudgeRetry:
    """_invoke_judge must retry on exception and fall back to score=1 after MAX_RETRIES."""

    def _call_invoke_judge(self, persona, side_effect):
        from src.nodes.judges import _invoke_judge, _PROSECUTOR_SYSTEM, _MAX_RETRIES

        evidence = Evidence(
            goal="git_forensic_analysis",
            found=True,
            location="git log",
            rationale="ok",
            confidence=0.9,
        )
        dim = _dim()

        structured = MagicMock()
        structured.invoke.side_effect = side_effect
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = structured

        with patch("src.nodes.judges.TRACKER"):
            opinion = _invoke_judge(persona, _PROSECUTOR_SYSTEM, dim, evidence, structured)
        return opinion, structured

    def test_returns_opinion_on_first_success(self):
        expected = _opinion()
        opinion, structured = self._call_invoke_judge(
            "Prosecutor", [expected]
        )
        assert opinion.score == 3
        assert opinion.criterion_id == "git_forensic_analysis"

    def test_retries_on_exception(self):
        from src.nodes.judges import _MAX_RETRIES
        expected = _opinion()
        # Fail twice then succeed
        side_effect = [Exception("bad"), Exception("bad"), expected] + [expected] * 10
        opinion, structured = self._call_invoke_judge("Prosecutor", side_effect)
        assert opinion.score == 3

    def test_fallback_opinion_after_max_retries(self):
        from src.nodes.judges import _MAX_RETRIES
        side_effect = [Exception("always fails")] * (_MAX_RETRIES + 5)
        opinion, _ = self._call_invoke_judge("Prosecutor", side_effect)
        assert opinion.score == 1
        assert "emission_failure" in opinion.cited_evidence
        assert opinion.judge == "Prosecutor"

    def test_fallback_judge_literal_preserved(self):
        from src.nodes.judges import _MAX_RETRIES
        side_effect = [Exception("x")] * (_MAX_RETRIES + 5)
        opinion, _ = self._call_invoke_judge("Defense", side_effect)
        assert opinion.judge == "Defense"


# ── Node output shape ─────────────────────────────────────────────────────────


class TestJudgeNodeOutput:
    """Each judge node must return {"opinions": [JudicialOpinion, ...]}."""

    def _run(self, node_fn, dims):
        state = _state(rubric_dimensions=dims)
        opinions_cycle = [_opinion(d["id"]) for d in dims] * 5
        structured = MagicMock()
        structured.invoke.side_effect = opinions_cycle
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = structured
        with patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm):
            with patch("src.nodes.judges.TRACKER"):
                return node_fn(state)

    def test_prosecutor_returns_opinions_list(self):
        from src.nodes.judges import prosecutor_node
        dims = [_dim("git_forensic_analysis"), _dim("state_management_rigor")]
        result = self._run(prosecutor_node, dims)
        assert "opinions" in result
        assert len(result["opinions"]) == 2

    def test_defense_returns_opinions_list(self):
        from src.nodes.judges import defense_node
        dims = [_dim("git_forensic_analysis")]
        result = self._run(defense_node, dims)
        assert "opinions" in result
        assert len(result["opinions"]) == 1

    def test_tech_lead_returns_opinions_list(self):
        from src.nodes.judges import tech_lead_node
        dims = [_dim("git_forensic_analysis")]
        result = self._run(tech_lead_node, dims)
        assert "opinions" in result

    def test_returns_empty_list_with_no_dimensions(self):
        from src.nodes.judges import prosecutor_node
        state = _state(rubric_dimensions=[])
        mock_llm = MagicMock()
        with patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm):
            with patch("src.nodes.judges.TRACKER"):
                result = prosecutor_node(state)
        assert result["opinions"] == []

    def test_opinion_criterion_id_matches_dimension(self):
        from src.nodes.judges import prosecutor_node
        dims = [_dim("safe_tool_engineering")]
        state = _state(rubric_dimensions=dims)
        expected_op = _opinion("safe_tool_engineering", "Prosecutor")
        structured = MagicMock()
        structured.invoke.return_value = expected_op
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = structured
        with patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm):
            with patch("src.nodes.judges.TRACKER"):
                result = prosecutor_node(state)
        assert result["opinions"][0].criterion_id == "safe_tool_engineering"

    def test_uses_placeholder_evidence_when_dimension_not_in_evidences(self):
        """When a dimension has no detective evidence, judges still produce an opinion."""
        from src.nodes.judges import prosecutor_node
        dims = [_dim("missing_dim")]
        state = _state(rubric_dimensions=dims, evidences={})
        expected_op = _opinion("missing_dim", "Prosecutor")
        structured = MagicMock()
        structured.invoke.return_value = expected_op
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = structured
        with patch("src.nodes.judges.ChatAnthropic", return_value=mock_llm):
            with patch("src.nodes.judges.TRACKER"):
                result = prosecutor_node(state)
        assert len(result["opinions"]) == 1
