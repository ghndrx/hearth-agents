"""Tests for cost tracking functionality."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hearth_agents.cost_tracker import CostTracker


class TestCostTracker:
    """Test suite for CostTracker class."""

    def test_init_creates_empty_tracker(self) -> None:
        """Test that a new tracker starts with empty costs."""
        tracker = CostTracker()
        assert tracker.get_all_costs() == {}

    def test_init_loads_existing_data(self, tmp_path: Path) -> None:
        """Test that existing cost data is loaded on startup."""
        costs_file = tmp_path / "costs.json"
        existing_data = {
            "feature-1": {
                "calls": [
                    {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cost_usd": 0.00045,
                        "model": "minimax-test",
                        "timestamp": "2024-01-01T00:00:00+00:00",
                    }
                ],
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "total_cost_usd": 0.00045,
            }
        }
        costs_file.write_text(json.dumps(existing_data))

        tracker = CostTracker(persist_path=str(costs_file))
        costs = tracker.get_feature_cost("feature-1")

        assert costs["total_input_tokens"] == 1000
        assert costs["total_output_tokens"] == 500
        assert costs["total_cost_usd"] == 0.00045
        assert costs["call_count"] == 1

    def test_on_llm_end_updates_costs(self, tmp_path: Path) -> None:
        """Test that LLM callbacks update cost tracking correctly."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        # Create a mock LLM result with token usage
        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
            "model_name": "MiniMax-M2.7",
        }
        mock_result.metadata = {"feature_id": "test-feature"}
        mock_result.generations = []

        # Simulate callback
        tracker.on_llm_end(mock_result, run_id="run-1")

        costs = tracker.get_feature_cost("test-feature")
        assert costs["total_input_tokens"] == 1000
        assert costs["total_output_tokens"] == 500
        assert costs["call_count"] == 1
        # MiniMax: (1000 * 0.15 + 500 * 0.60) / 1_000_000 = 0.00045
        assert costs["total_cost_usd"] == pytest.approx(0.00045, rel=1e-5)

    def test_kimi_pricing_applied(self, tmp_path: Path) -> None:
        """Test that Kimi pricing is correctly applied."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 1000,
            },
            "model_name": "kimi-for-coding",
        }
        mock_result.metadata = {"feature_id": "kimi-feature"}
        mock_result.generations = []

        tracker.on_llm_end(mock_result, run_id="run-1")

        costs = tracker.get_feature_cost("kimi-feature")
        # Kimi: (2000 * 3.00 + 1000 * 12.00) / 1_000_000 = 0.018
        assert costs["total_cost_usd"] == pytest.approx(0.018, rel=1e-5)

    def test_check_budget_under_budget(self, tmp_path: Path) -> None:
        """Test that check_budget returns True when under budget."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
            "model_name": "minimax",
        }
        mock_result.metadata = {"feature_id": "budget-feature"}
        mock_result.generations = []

        tracker.on_llm_end(mock_result, run_id="run-1")

        assert tracker.check_budget("budget-feature") is True

    def test_check_budget_over_budget(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that check_budget returns False when budget exceeded."""
        # Set a very low budget
        from hearth_agents import config
        monkeypatch.setattr(config.settings, "per_feature_budget_usd", 0.00001)

        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
            "model_name": "minimax",
        }
        mock_result.metadata = {"feature_id": "over-budget-feature"}
        mock_result.generations = []

        tracker.on_llm_end(mock_result, run_id="run-1")

        assert tracker.check_budget("over-budget-feature") is False

    def test_check_budget_unknown_feature(self, tmp_path: Path) -> None:
        """Test that check_budget returns True for unknown features."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))
        assert tracker.check_budget("unknown-feature") is True

    def test_reset_feature_clears_costs(self, tmp_path: Path) -> None:
        """Test that reset_feature clears costs for a feature."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
            "model_name": "minimax",
        }
        mock_result.metadata = {"feature_id": "reset-feature"}
        mock_result.generations = []

        tracker.on_llm_end(mock_result, run_id="run-1")
        assert tracker.get_feature_cost("reset-feature")["call_count"] == 1

        # Reset and verify
        import asyncio
        was_reset = asyncio.run(tracker.reset_feature("reset-feature"))
        assert was_reset is True
        assert tracker.get_feature_cost("reset-feature")["call_count"] == 0

    def test_reset_feature_unknown(self, tmp_path: Path) -> None:
        """Test that reset_feature returns False for unknown features."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        import asyncio
        was_reset = asyncio.run(tracker.reset_feature("unknown-feature"))
        assert was_reset is False

    def test_get_feature_cost_unknown(self, tmp_path: Path) -> None:
        """Test that get_feature_cost returns empty data for unknown features."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))
        costs = tracker.get_feature_cost("unknown-feature")

        assert costs["feature_id"] == "unknown-feature"
        assert costs["total_input_tokens"] == 0
        assert costs["total_output_tokens"] == 0
        assert costs["total_cost_usd"] == 0.0
        assert costs["call_count"] == 0
        assert costs["calls"] == []

    def test_multiple_calls_accumulate(self, tmp_path: Path) -> None:
        """Test that multiple LLM calls accumulate correctly."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        for i in range(3):
            mock_result = MagicMock()
            mock_result.llm_output = {
                "token_usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
                "model_name": "minimax",
            }
            mock_result.metadata = {"feature_id": "multi-call-feature"}
            mock_result.generations = []
            tracker.on_llm_end(mock_result, run_id=f"run-{i}")

        costs = tracker.get_feature_cost("multi-call-feature")
        assert costs["call_count"] == 3
        assert costs["total_input_tokens"] == 3000
        assert costs["total_output_tokens"] == 1500
        assert costs["total_cost_usd"] == pytest.approx(0.00135, rel=1e-5)

    async def test_persistence(self, tmp_path: Path) -> None:
        """Test that costs are persisted to disk."""
        costs_file = tmp_path / "costs.json"

        # Create tracker and add costs
        tracker1 = CostTracker(persist_path=str(costs_file))

        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
            "model_name": "minimax",
        }
        mock_result.metadata = {"feature_id": "persist-feature"}
        mock_result.generations = []
        tracker1.on_llm_end(mock_result, run_id="run-1")

        # Manually trigger save to ensure persistence
        await tracker1._save()

        # Create new tracker and verify data is loaded
        tracker2 = CostTracker(persist_path=str(costs_file))
        costs = tracker2.get_feature_cost("persist-feature")
        assert costs["call_count"] == 1
        assert costs["total_input_tokens"] == 1000

    def test_unknown_model_defaults_to_minimax(self, tmp_path: Path) -> None:
        """Test that unknown models default to MiniMax pricing."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        mock_result = MagicMock()
        mock_result.llm_output = {
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
            "model_name": "unknown-model",
        }
        mock_result.metadata = {"feature_id": "unknown-model-feature"}
        mock_result.generations = []

        tracker.on_llm_end(mock_result, run_id="run-1")

        costs = tracker.get_feature_cost("unknown-model-feature")
        # Should use MiniMax pricing
        assert costs["total_cost_usd"] == pytest.approx(0.00045, rel=1e-5)

    def test_fallback_token_estimation(self, tmp_path: Path) -> None:
        """Test fallback token estimation when no usage data available."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        mock_result = MagicMock()
        mock_result.llm_output = None
        mock_result.metadata = {"feature_id": "fallback-feature"}

        # Mock generation with text
        mock_gen = MagicMock()
        mock_gen.text = "a" * 400  # ~100 tokens at 4 chars per token
        mock_result.generations = [[mock_gen]]

        tracker.on_llm_end(mock_result, run_id="run-1")

        costs = tracker.get_feature_cost("fallback-feature")
        # Should have estimated output tokens from generation text
        assert costs["total_output_tokens"] == 100


class TestCostTrackerIntegration:
    """Integration tests for CostTracker."""

    @pytest.mark.asyncio
    async def test_record_manual(self, tmp_path: Path) -> None:
        """Test manual cost recording."""
        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        await tracker.record_manual(
            feature_id="manual-feature",
            input_tokens=2000,
            output_tokens=1000,
            model="minimax",
        )

        costs = tracker.get_feature_cost("manual-feature")
        assert costs["total_input_tokens"] == 2000
        assert costs["total_output_tokens"] == 1000
        assert costs["call_count"] == 1

    @pytest.mark.asyncio
    async def test_concurrent_updates(self, tmp_path: Path) -> None:
        """Test thread-safety with concurrent updates."""
        import asyncio

        tracker = CostTracker(persist_path=str(tmp_path / "costs.json"))

        async def add_record(i: int) -> None:
            await tracker.record_manual(
                feature_id="concurrent-feature",
                input_tokens=100,
                output_tokens=50,
                model="minimax",
            )

        # Run multiple concurrent updates
        await asyncio.gather(*[add_record(i) for i in range(10)])

        costs = tracker.get_feature_cost("concurrent-feature")
        assert costs["call_count"] == 10
        assert costs["total_input_tokens"] == 1000
        assert costs["total_output_tokens"] == 500
