"""Tests for src/utils/spend_tracker.py.

Covers: pricing table model-name matching, on_llm_end with usage_metadata,
on_llm_end fallback to response_metadata, model name extraction, and
aggregation helpers.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.utils.spend_tracker import TRACKER, CallRecord, SpendTracker, _price_per_token, _PRICING


# ── Pricing table ────────────────────────────────────────────────────────────


class TestPricePerToken:
    """_price_per_token must match current and legacy model name patterns."""

    def test_haiku_4_5_matches(self):
        rates = _price_per_token("claude-haiku-4-5-20251001")
        assert rates["input"] == pytest.approx(0.80 / 1_000_000)
        assert rates["output"] == pytest.approx(4.00 / 1_000_000)

    def test_haiku_4_matches(self):
        rates = _price_per_token("claude-haiku-4-20260101")
        assert rates["input"] == pytest.approx(0.80 / 1_000_000)

    def test_haiku_legacy_3_5_matches(self):
        rates = _price_per_token("claude-3-5-haiku-20241022")
        assert rates["input"] == pytest.approx(0.80 / 1_000_000)

    def test_haiku_legacy_3_matches(self):
        rates = _price_per_token("claude-3-haiku-20240307")
        assert rates["input"] == pytest.approx(0.80 / 1_000_000)

    def test_sonnet_3_5_matches(self):
        rates = _price_per_token("claude-3-5-sonnet-20241022")
        assert rates["input"] == pytest.approx(3.00 / 1_000_000)
        assert rates["output"] == pytest.approx(15.00 / 1_000_000)

    def test_sonnet_4_matches(self):
        rates = _price_per_token("claude-sonnet-4-20260101")
        assert rates["input"] == pytest.approx(3.00 / 1_000_000)

    def test_opus_3_matches(self):
        rates = _price_per_token("claude-3-opus-20240229")
        assert rates["input"] == pytest.approx(15.00 / 1_000_000)
        assert rates["output"] == pytest.approx(75.00 / 1_000_000)

    def test_opus_4_matches(self):
        rates = _price_per_token("claude-opus-4-20260201")
        assert rates["input"] == pytest.approx(15.00 / 1_000_000)

    def test_unknown_model_uses_default(self):
        rates = _price_per_token("some-totally-unknown-llm-v99")
        assert rates == _PRICING["default"]

    def test_matching_is_case_insensitive(self):
        assert _price_per_token("CLAUDE-HAIKU-4-5") == _price_per_token("claude-haiku-4-5")

    def test_default_key_not_matched_as_substring(self):
        """'default' must not accidentally match a model named 'default-something'."""
        rates = _price_per_token("default-model")
        # Should use the default rates (fallback), not error out
        assert "input" in rates and "output" in rates


# ── CallRecord cost calculations ─────────────────────────────────────────────


class TestCallRecord:
    def _record(self, model="claude-haiku-4-5-20251001", inp=1000, out=500):
        return CallRecord(model=model, node="test", input_tokens=inp, output_tokens=out)

    def test_input_cost_calculated_correctly(self):
        r = self._record(inp=1_000_000)
        assert r.input_cost == pytest.approx(0.80)

    def test_output_cost_calculated_correctly(self):
        r = self._record(out=1_000_000)
        assert r.output_cost == pytest.approx(4.00)

    def test_total_cost_is_sum(self):
        r = self._record(inp=1000, out=500)
        assert r.total_cost == pytest.approx(r.input_cost + r.output_cost)

    def test_as_dict_contains_expected_keys(self):
        d = self._record().as_dict()
        for key in ("model", "node", "input_tokens", "output_tokens",
                    "input_cost_usd", "output_cost_usd", "total_cost_usd"):
            assert key in d


# ── SpendTracker.on_llm_end ───────────────────────────────────────────────────


def _make_llm_result(
    usage_metadata: dict | None = None,
    response_metadata: dict | None = None,
    model: str = "claude-haiku-4-5-20251001",
):
    """Build a minimal LLMResult-like object for on_llm_end tests."""
    msg = MagicMock()
    msg.usage_metadata = usage_metadata
    msg.response_metadata = response_metadata or {"model": model}

    gen = MagicMock()
    gen.message = msg

    response = MagicMock()
    response.generations = [[gen]]
    return response


class TestOnLlmEnd:
    def setup_method(self):
        self.tracker = SpendTracker()
        self.tracker.set_node("test_node")

    def test_records_tokens_from_usage_metadata(self):
        response = _make_llm_result(
            usage_metadata={"input_tokens": 100, "output_tokens": 50},
        )
        self.tracker.on_llm_end(response)
        assert len(self.tracker.records) == 1
        assert self.tracker.records[0].input_tokens == 100
        assert self.tracker.records[0].output_tokens == 50

    def test_falls_back_to_response_metadata_when_no_usage_metadata(self):
        response = _make_llm_result(
            usage_metadata=None,
            response_metadata={"usage": {"input_tokens": 200, "output_tokens": 75},
                               "model": "claude-3-5-haiku-20241022"},
        )
        self.tracker.on_llm_end(response)
        assert len(self.tracker.records) == 1
        assert self.tracker.records[0].input_tokens == 200
        assert self.tracker.records[0].output_tokens == 75

    def test_records_model_name_from_response_metadata(self):
        response = _make_llm_result(
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
            response_metadata={"model": "claude-haiku-4-5-20251001"},
        )
        self.tracker.on_llm_end(response)
        assert self.tracker.records[0].model == "claude-haiku-4-5-20251001"

    def test_no_record_when_zero_tokens(self):
        response = _make_llm_result(
            usage_metadata={"input_tokens": 0, "output_tokens": 0},
        )
        self.tracker.on_llm_end(response)
        assert len(self.tracker.records) == 0

    def test_tags_record_with_active_node(self):
        self.tracker.set_node("my_judge_node")
        response = _make_llm_result(
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
        )
        self.tracker.on_llm_end(response)
        assert self.tracker.records[0].node == "my_judge_node"

    def test_unknown_model_falls_back_to_string(self):
        msg = MagicMock()
        msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5}
        msg.response_metadata = {}
        gen = MagicMock()
        gen.message = msg
        response = MagicMock()
        response.generations = [[gen]]
        self.tracker.on_llm_end(response)
        assert self.tracker.records[0].model == "unknown"


# ── summary / report ──────────────────────────────────────────────────────────


class TestSummaryReport:
    def setup_method(self):
        self.tracker = SpendTracker()

    def test_summary_empty(self):
        s = self.tracker.summary()
        assert s["total_calls"] == 0
        assert s["total_tokens"] == 0
        assert s["total_cost_usd"] == 0.0

    def test_summary_accumulates_records(self):
        self.tracker.records.append(
            CallRecord(model="claude-haiku-4-5-20251001", node="n1",
                       input_tokens=1000, output_tokens=500)
        )
        self.tracker.records.append(
            CallRecord(model="claude-haiku-4-5-20251001", node="n1",
                       input_tokens=500, output_tokens=250)
        )
        s = self.tracker.summary()
        assert s["total_calls"] == 2
        assert s["total_input_tokens"] == 1500
        assert s["total_output_tokens"] == 750

    def test_by_node_grouping(self):
        self.tracker.records.append(
            CallRecord(model="claude-haiku-4-5-20251001", node="nodeA",
                       input_tokens=100, output_tokens=50)
        )
        self.tracker.records.append(
            CallRecord(model="claude-haiku-4-5-20251001", node="nodeB",
                       input_tokens=200, output_tokens=100)
        )
        s = self.tracker.summary()
        assert "nodeA" in s["by_node"]
        assert "nodeB" in s["by_node"]
        assert s["by_node"]["nodeA"]["calls"] == 1

    def test_reset_clears_records(self):
        self.tracker.records.append(
            CallRecord(model="m", node="n", input_tokens=1, output_tokens=1)
        )
        self.tracker.reset()
        assert self.tracker.summary()["total_calls"] == 0

    def test_report_is_string(self):
        assert isinstance(self.tracker.report(), str)

    def test_report_contains_totals(self):
        self.tracker.records.append(
            CallRecord(model="claude-haiku-4-5-20251001", node="n",
                       input_tokens=1000, output_tokens=500)
        )
        report = self.tracker.report()
        assert "1" in report  # total calls
        assert "1,000" in report or "1000" in report  # input tokens
