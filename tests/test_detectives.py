"""Tests for src/nodes/detectives.py.

Structure
---------
1. TestIsNotFound            — unit tests for the _is_not_found() helper
2. TestDetectiveRules        — system prompt content (batching instruction)
3. TestRunDetectiveAgentLoop — loop mechanics: missing-path short-circuit,
                               early exit on empty rounds, emission fallback
4. TestRepoInvestigatorNode  — node-level orchestration with mocked agent loop
5. TestDocAnalystNode        — node-level orchestration with mocked agent loop
6. TestVisionInspectorNode   — node-level orchestration (VISION_ENABLED guard)

No real LLM calls, no network, no filesystem access.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.nodes.detectives import (
    _DETECTIVE_RULES,
    _build_system_prompt,
    _is_not_found,
    _run_detective_agent,
    doc_analyst_node,
    repo_investigator_node,
    vision_inspector_node,
)
from src.state import Evidence
from src.tools.repo_tools import CloneResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dim(id_="test_dim", target="github_repo"):
    return {
        "id": id_,
        "name": f"Test {id_}",
        "target_artifact": target,
        "forensic_instruction": "Scan for X.",
        "success_pattern": "X is present",
        "failure_pattern": "X is absent",
    }


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


def _evidence(goal="test_dim", found=True, confidence=0.9):
    return Evidence(
        goal=goal,
        found=found,
        location="src/x.py",
        rationale="Rationale.",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# 1. _is_not_found
# ---------------------------------------------------------------------------


class TestIsNotFound:
    def test_ok_false_signals_not_found(self):
        assert _is_not_found({"ok": False, "error": "file not found"}) is True

    def test_exists_false_signals_not_found(self):
        assert _is_not_found({"ok": True, "exists": False}) is True

    def test_ok_true_with_data_is_productive(self):
        assert _is_not_found({"ok": True, "files": ["src/state.py"]}) is False

    def test_ok_true_exists_true_is_productive(self):
        assert _is_not_found({"ok": True, "exists": True, "patterns_found": {}}) is False

    def test_non_dict_is_productive(self):
        assert _is_not_found("some string") is False
        assert _is_not_found(None) is False
        assert _is_not_found(42) is False

    def test_error_key_alone_is_not_enough(self):
        # "error" without ok=False is not a clear not-found signal
        assert _is_not_found({"error": "something"}) is False


# ---------------------------------------------------------------------------
# 2. _DETECTIVE_RULES / _build_system_prompt
# ---------------------------------------------------------------------------


class TestDetectiveRules:
    def test_batching_instruction_present(self):
        lower = _DETECTIVE_RULES.lower()
        assert "simultaneously" in lower or "batch" in lower, (
            "_DETECTIVE_RULES should instruct the LLM to batch independent tool calls"
        )

    def test_sequential_exception_mentioned(self):
        lower = _DETECTIVE_RULES.lower()
        assert "sequentially" in lower or "depend" in lower, (
            "_DETECTIVE_RULES should explain when sequential calls are acceptable"
        )

    def test_build_system_prompt_embeds_dimension_fields(self):
        dimension = _dim()
        prompt = _build_system_prompt([dimension])
        assert dimension["id"] in prompt
        assert dimension["forensic_instruction"] in prompt
        assert dimension["success_pattern"] in prompt
        assert dimension["failure_pattern"] in prompt

    def test_build_system_prompt_includes_detective_rules(self):
        prompt = _build_system_prompt([_dim()])
        # A distinctive phrase from _DETECTIVE_RULES should appear
        assert "forensic investigator" in prompt.lower()


# ---------------------------------------------------------------------------
# 3. _run_detective_agent loop mechanics
# ---------------------------------------------------------------------------


class TestRunDetectiveAgentLoop:
    """Mock ChatAnthropic to test the tool-calling loop without real LLM calls.

    ChatAnthropic is instantiated twice inside _run_detective_agent:
      1. llm = ChatAnthropic(...)  →  used for the tool-calling loop
      2. ChatAnthropic(...).with_structured_output(Evidence)  →  emission
    We use side_effect=[loop_mock, emit_mock] on the constructor to serve each.
    """

    def _make_tool(self, name: str, return_value: dict) -> MagicMock:
        t = MagicMock()
        t.name = name
        t.invoke = MagicMock(return_value=return_value)
        return t

    def _make_tc(self, name: str, args: dict, tc_id: str = "tc1") -> dict:
        return {"name": name, "args": args, "id": tc_id}

    def _make_response(self, tool_calls=None) -> MagicMock:
        resp = MagicMock()
        resp.tool_calls = tool_calls or []
        return resp

    def _run(self, loop_responses, tools, emission_ev=None) -> Evidence:
        """Run _run_detective_agent with fully mocked LLM."""
        if emission_ev is None:
            emission_ev = _evidence()

        loop_mock = MagicMock()
        loop_mock.bind_tools.return_value.invoke.side_effect = loop_responses

        emit_mock = MagicMock()
        emit_mock.with_structured_output.return_value.invoke.return_value = emission_ev

        with (
            patch("src.nodes.detectives.ChatAnthropic", side_effect=[loop_mock, emit_mock]),
            patch("src.nodes.detectives.TRACKER"),
            patch("src.nodes.detectives.LOGGER"),
        ):
            return _run_detective_agent(
                dimension=_dim(),
                user_message="Audit this.",
                tools=tools,
                node_name="test_node",
            )

    # ── normal completion ────────────────────────────────────────────────

    def test_returns_evidence_on_normal_completion(self):
        tool = self._make_tool("scan_file_for_patterns", {"ok": True, "exists": True, "patterns_found": {"X": True}})
        tc = self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": "src/x.py", "patterns": ["X"]})

        responses = [
            self._make_response([tc]),  # round 1: tool called
            self._make_response([]),    # round 2: no tools → loop exits
        ]
        ev = self._run(responses, [tool])

        assert ev.goal == "test_dim"
        assert ev.found is True
        assert ev.confidence == pytest.approx(0.9)

    def test_tool_invoke_called_once_on_normal_completion(self):
        tool = self._make_tool("scan_file_for_patterns", {"ok": True, "exists": True, "patterns_found": {}})
        tc = self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": "src/x.py", "patterns": []})

        responses = [self._make_response([tc]), self._make_response([])]
        self._run(responses, [tool])

        assert tool.invoke.call_count == 1

    # ── missing-path short-circuit ───────────────────────────────────────

    def test_missing_path_not_re_scanned(self):
        """After a file is confirmed absent, a second scan of the same path is
        short-circuited — the underlying tool is never called again."""
        tool = self._make_tool(
            "scan_file_for_patterns",
            {"ok": False, "exists": False, "error": "File not found: src/judges.py"},
        )
        tc_first = self._make_tc(
            "scan_file_for_patterns",
            {"cloned_path": "/tmp", "relative_path": "src/judges.py", "patterns": ["class"]},
            tc_id="tc1",
        )
        tc_repeat = self._make_tc(
            "scan_file_for_patterns",
            {"cloned_path": "/tmp", "relative_path": "src/judges.py", "patterns": ["def"]},
            tc_id="tc2",
        )

        responses = [
            self._make_response([tc_first]),   # round 1: real call → not-found, path recorded
            self._make_response([tc_repeat]),  # round 2: same path → short-circuited
            self._make_response([]),           # round 3: done
        ]
        self._run(responses, [tool])

        # Tool.invoke should only have been called once (round 1 only)
        assert tool.invoke.call_count == 1

    def test_short_circuit_still_appends_tool_message(self):
        """Short-circuited call must still produce a ToolMessage so the LLM
        conversation stays valid (no orphaned tool_call_id)."""
        tool = self._make_tool(
            "scan_file_for_patterns",
            {"ok": False, "exists": False, "error": "not found"},
        )
        tc1 = self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": "src/judges.py", "patterns": []}, "tc1")
        tc2 = self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": "src/judges.py", "patterns": []}, "tc2")

        responses = [self._make_response([tc1]), self._make_response([tc2]), self._make_response([])]

        # Should complete without raising (malformed conversation raises inside LangChain)
        ev = self._run(responses, [tool])
        assert ev is not None

    # ── early exit on consecutive empty rounds ───────────────────────────

    def test_early_exit_after_max_empty_rounds(self):
        """When all tool results are non-productive for _MAX_EMPTY_ROUNDS consecutive
        rounds the loop exits early rather than exhausting _MAX_ITERATIONS."""
        # Use distinct paths so missing-path short-circuit doesn't interfere
        tool = self._make_tool(
            "scan_file_for_patterns",
            {"ok": False, "exists": False, "error": "not found"},
        )
        tcs = [
            self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": f"src/missing_{i}.py", "patterns": []}, f"tc{i}")
            for i in range(8)
        ]
        # Provide 8 rounds of responses — early exit should fire after 2
        responses = [self._make_response([tc]) for tc in tcs]

        with patch("src.nodes.detectives._MAX_EMPTY_ROUNDS", 2):
            self._run(responses, [tool])

        # With early exit after 2 rounds, tool.invoke called at most 2 times
        assert tool.invoke.call_count <= 2

    def test_early_exit_counter_resets_on_productive_round(self):
        """A productive round in between two empty rounds should reset the
        counter, preventing a premature early exit."""
        fail_tool = self._make_tool("scan_file_for_patterns", {"ok": False, "exists": False, "error": "not found"})
        ok_tool = self._make_tool("list_repo_files", {"ok": True, "files": ["src/state.py"]})

        tc_fail_a = self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": "src/a.py", "patterns": []}, "tc1")
        tc_ok = self._make_tc("list_repo_files", {"cloned_path": "/tmp", "glob_pattern": "**/*.py"}, "tc2")
        tc_fail_b = self._make_tc("scan_file_for_patterns", {"cloned_path": "/tmp", "relative_path": "src/b.py", "patterns": []}, "tc3")

        responses = [
            self._make_response([tc_fail_a]),  # round 1: all fail → empty_count=1
            self._make_response([tc_ok]),      # round 2: productive → empty_count reset to 0
            self._make_response([tc_fail_b]),  # round 3: all fail → empty_count=1 (not 2)
            self._make_response([]),           # round 4: natural exit
        ]

        with patch("src.nodes.detectives._MAX_EMPTY_ROUNDS", 2):
            self._run(responses, [fail_tool, ok_tool])

        # ok_tool.invoke was called in round 2, so the run reached round 3
        assert ok_tool.invoke.call_count == 1

    # ── emission fallback ────────────────────────────────────────────────

    def test_fallback_evidence_on_emission_failure(self):
        """If all 3 emission attempts raise, a fallback Evidence with
        found=False and confidence=0 is returned instead of raising."""
        tool = self._make_tool("list_repo_files", {"ok": True, "files": []})
        responses = [self._make_response([]), ]  # immediate natural exit

        loop_mock = MagicMock()
        loop_mock.bind_tools.return_value.invoke.side_effect = responses

        emit_mock = MagicMock()
        emit_mock.with_structured_output.return_value.invoke.side_effect = ValueError("bad schema")

        with (
            patch("src.nodes.detectives.ChatAnthropic", side_effect=[loop_mock, emit_mock]),
            patch("src.nodes.detectives.TRACKER"),
            patch("src.nodes.detectives.LOGGER"),
        ):
            ev = _run_detective_agent(
                dimension=_dim(),
                user_message="Audit.",
                tools=[tool],
                node_name="test_node",
            )

        assert ev.found is False
        assert ev.confidence == pytest.approx(0.0)
        assert "failed" in ev.rationale.lower()


# ---------------------------------------------------------------------------
# 4. RepoInvestigatorNode — node-level orchestration
# ---------------------------------------------------------------------------


class TestRepoInvestigatorNode:
    def test_empty_list_when_no_repo_dimensions(self):
        """No github_repo dimensions → return empty evidence list without error."""
        state = _base_state(rubric_dimensions=[_dim(target="pdf_report")])
        result = repo_investigator_node(state)
        assert result["evidences"]["repo"] == []

    def test_found_false_when_no_repo_url(self):
        """Missing repo_url → found=False for every dimension without LLM call."""
        dims = [_dim("git_forensic_analysis"), _dim("safe_tool_engineering")]
        state = _base_state(repo_url="", rubric_dimensions=dims)
        result = repo_investigator_node(state)
        assert all(not e.found for e in result["evidences"]["repo"])

    def test_found_false_for_all_dims_on_clone_failure(self):
        """Clone failure → found=False for every dimension, no LLM calls."""
        clone_fail = CloneResult(ok=False, error="clone_failed", details="Not found")
        dims = [_dim("git_forensic_analysis"), _dim("graph_orchestration")]
        state = _base_state(rubric_dimensions=dims)

        with patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_fail):
            result = repo_investigator_node(state)

        evidences = result["evidences"]["repo"]
        assert len(evidences) == 2
        assert all(not e.found for e in evidences)

    def test_one_evidence_per_dimension_on_success(self):
        """One Evidence emitted per rubric dimension after successful clone."""
        clone_ok = CloneResult(ok=True, cloned_path="/tmp/repo")
        dims = [_dim("git_forensic_analysis"), _dim("graph_orchestration")]
        state = _base_state(rubric_dimensions=dims)

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_ok),
            patch(
                "src.nodes.detectives._run_detective_agent",
                side_effect=[_evidence("git_forensic_analysis"), _evidence("graph_orchestration")],
            ),
        ):
            result = repo_investigator_node(state)

        evidences = result["evidences"]["repo"]
        assert len(evidences) == 2
        assert {e.goal for e in evidences} == {"git_forensic_analysis", "graph_orchestration"}

    def test_dimension_failure_does_not_block_others(self):
        """If the agent loop raises for one dimension, a found=False evidence is
        recorded and the remaining dimensions are still processed."""
        clone_ok = CloneResult(ok=True, cloned_path="/tmp/repo")
        dims = [_dim("git_forensic_analysis"), _dim("graph_orchestration")]
        state = _base_state(rubric_dimensions=dims)

        with (
            patch("src.nodes.detectives.clone_repo_sandboxed", return_value=clone_ok),
            patch(
                "src.nodes.detectives._run_detective_agent",
                side_effect=[RuntimeError("boom"), _evidence("graph_orchestration")],
            ),
        ):
            result = repo_investigator_node(state)

        evidences = result["evidences"]["repo"]
        goals = {e.goal for e in evidences}
        assert "git_forensic_analysis" in goals
        assert "graph_orchestration" in goals
        git_ev = next(e for e in evidences if e.goal == "git_forensic_analysis")
        assert git_ev.found is False


# ---------------------------------------------------------------------------
# 5. DocAnalystNode — node-level orchestration
# ---------------------------------------------------------------------------


class TestDocAnalystNode:
    def test_empty_list_when_no_pdf_dimensions(self):
        state = _base_state(rubric_dimensions=[_dim(target="github_repo")])
        result = doc_analyst_node(state)
        assert result["evidences"]["doc"] == []

    def test_found_false_when_pdf_missing(self):
        dims = [_dim("theoretical_depth", "pdf_report")]
        state = _base_state(pdf_path="/nonexistent/path.pdf", rubric_dimensions=dims)
        with patch("pathlib.Path.exists", return_value=False):
            result = doc_analyst_node(state)
        assert all(not e.found for e in result["evidences"]["doc"])

    def test_one_evidence_per_pdf_dimension(self):
        dims = [_dim("theoretical_depth", "pdf_report"), _dim("report_accuracy", "pdf_report")]
        state = _base_state(rubric_dimensions=dims)

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "src.nodes.detectives._run_detective_agent",
                side_effect=[
                    _evidence("theoretical_depth"),
                    _evidence("report_accuracy"),
                ],
            ),
        ):
            result = doc_analyst_node(state)

        assert len(result["evidences"]["doc"]) == 2
        assert {e.goal for e in result["evidences"]["doc"]} == {"theoretical_depth", "report_accuracy"}


# ---------------------------------------------------------------------------
# 6. VisionInspectorNode
# ---------------------------------------------------------------------------


class TestVisionInspectorNode:
    def test_skips_entirely_when_vision_disabled(self):
        """When VISION_ENABLED=false the node returns found=False evidence
        without making any LLM or tool calls."""
        dims = [_dim("swarm_visual", "pdf_images")]
        state = _base_state(rubric_dimensions=dims)

        with (
            patch("src.nodes.detectives._vision_enabled", return_value=False),
            patch("src.nodes.detectives._run_detective_agent") as mock_agent,
        ):
            result = vision_inspector_node(state)

        mock_agent.assert_not_called()
        ev = result["evidences"]["vision"][0]
        assert ev.found is False
        assert "disabled" in ev.rationale.lower()

    def test_empty_list_when_no_vision_dimensions(self):
        """No pdf_images dimensions → empty list, vision enabled."""
        state = _base_state(rubric_dimensions=[_dim(target="github_repo")])
        with patch("src.nodes.detectives._vision_enabled", return_value=True):
            result = vision_inspector_node(state)
        assert result["evidences"]["vision"] == []

    def test_found_false_when_pdf_missing_and_vision_enabled(self):
        dims = [_dim("swarm_visual", "pdf_images")]
        state = _base_state(pdf_path="/nonexistent.pdf", rubric_dimensions=dims)
        with (
            patch("src.nodes.detectives._vision_enabled", return_value=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            result = vision_inspector_node(state)
        assert all(not e.found for e in result["evidences"]["vision"])
