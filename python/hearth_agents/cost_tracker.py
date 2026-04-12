"""Cost tracking for LLM calls using LangChain callbacks.

Tracks input/output tokens and costs per feature for MiniMax and Kimi LLMs.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler

from .config import settings
from .logger import log

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult


# Pricing per 1M tokens (in USD)
MINIMAX_INPUT_PRICE = 0.15
MINIMAX_OUTPUT_PRICE = 0.60
KIMI_INPUT_PRICE = 3.00
KIMI_OUTPUT_PRICE = 12.00


@dataclass
class CostRecord:
    """Cost record for a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    timestamp: str = field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())


@dataclass
class FeatureCosts:
    """Aggregated costs for a feature."""

    calls: list[CostRecord] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    def add_call(self, record: CostRecord) -> None:
        """Add a cost record and update totals."""
        self.calls.append(record)
        self.total_input_tokens += record.input_tokens
        self.total_output_tokens += record.output_tokens
        self.total_cost_usd += record.cost_usd


class CostTracker(BaseCallbackHandler):
    """LangChain callback handler for tracking LLM costs per feature.

    Tracks token usage and costs for MiniMax and Kimi models, persisting
    data to JSON for budget monitoring and analysis.
    """

    def __init__(self, persist_path: str | None = None) -> None:
        """Initialize the cost tracker.

        Args:
            persist_path: Path to JSON file for persistence. Defaults to settings.costs_path.
        """
        super().__init__()
        self._path = Path(persist_path) if persist_path else Path(settings.costs_path)
        self._costs: dict[str, FeatureCosts] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        """Load existing cost data from disk."""
        if not self._path.exists():
            log.info("cost_tracker.no_existing_data", path=str(self._path))
            return

        try:
            data = json.loads(self._path.read_text())
            for feature_id, feature_data in data.items():
                calls = [CostRecord(**c) for c in feature_data.get("calls", [])]
                self._costs[feature_id] = FeatureCosts(
                    calls=calls,
                    total_input_tokens=feature_data.get("total_input_tokens", 0),
                    total_output_tokens=feature_data.get("total_output_tokens", 0),
                    total_cost_usd=feature_data.get("total_cost_usd", 0.0),
                )
            log.info("cost_tracker.loaded", features=len(self._costs), path=str(self._path))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("cost_tracker.load_failed", error=str(e), path=str(self._path))
            self._costs = {}

    async def _save(self) -> None:
        """Persist cost data to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                feature_id: {
                    "calls": [asdict(c) for c in fc.calls],
                    "total_input_tokens": fc.total_input_tokens,
                    "total_output_tokens": fc.total_output_tokens,
                    "total_cost_usd": fc.total_cost_usd,
                }
                for feature_id, fc in self._costs.items()
            }
            # Use run_in_executor for file I/O to avoid blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._path.write_text(json.dumps(data, indent=2))
            )
            log.debug("cost_tracker.saved", path=str(self._path))
        except OSError as e:
            log.error("cost_tracker.save_failed", error=str(e), path=str(self._path))

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost in USD based on model and token counts.

        Args:
            model: Model identifier (case-insensitive).
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Cost in USD.
        """
        model_lower = model.lower()
        if "minimax" in model_lower:
            input_price = MINIMAX_INPUT_PRICE
            output_price = MINIMAX_OUTPUT_PRICE
        elif "kimi" in model_lower:
            input_price = KIMI_INPUT_PRICE
            output_price = KIMI_OUTPUT_PRICE
        else:
            # Default to MiniMax pricing for unknown models
            log.warning("cost_tracker.unknown_model", model=model)
            input_price = MINIMAX_INPUT_PRICE
            output_price = MINIMAX_OUTPUT_PRICE

        input_cost = (input_tokens / 1_000_000) * input_price
        output_cost = (output_tokens / 1_000_000) * output_price
        return round(input_cost + output_cost, 6)

    def on_llm_end(self, response: LLMResult, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any) -> None:
        """Callback when LLM call completes.

        Extracts token usage from response and records cost.
        """
        # Extract feature_id from run metadata if available
        feature_id = "default"
        if hasattr(response, "metadata") and response.metadata:
            feature_id = response.metadata.get("feature_id", feature_id)

        # Try to get token usage from response
        input_tokens = 0
        output_tokens = 0
        model = "unknown"

        if response.llm_output:
            llm_output = response.llm_output
            # OpenAI-compatible format
            usage = llm_output.get("token_usage") or llm_output.get("usage", {})
            if usage:
                input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
            model = llm_output.get("model_name", llm_output.get("model", "unknown"))

        # Fallback: estimate from generations if no usage data
        if input_tokens == 0 and output_tokens == 0 and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    if hasattr(gen, "text") and gen.text:
                        # Rough estimate: ~4 chars per token
                        output_tokens += len(gen.text) // 4

        cost = self._calculate_cost(model, input_tokens, output_tokens)
        record = CostRecord(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            model=model,
        )

        # Schedule the async update
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._add_record(feature_id, record))
            else:
                # Synchronous fallback for non-async contexts
                if feature_id not in self._costs:
                    self._costs[feature_id] = FeatureCosts()
                self._costs[feature_id].add_call(record)
        except RuntimeError:
            # No event loop running
            if feature_id not in self._costs:
                self._costs[feature_id] = FeatureCosts()
            self._costs[feature_id].add_call(record)

    async def _add_record(self, feature_id: str, record: CostRecord) -> None:
        """Thread-safe addition of a cost record."""
        async with self._lock:
            if feature_id not in self._costs:
                self._costs[feature_id] = FeatureCosts()
            self._costs[feature_id].add_call(record)
            await self._save()

    def get_feature_cost(self, feature_id: str) -> dict[str, Any]:
        """Get cost summary for a feature.

        Args:
            feature_id: The feature identifier.

        Returns:
            Dictionary with cost summary including totals and call history.
        """
        if feature_id not in self._costs:
            return {
                "feature_id": feature_id,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0.0,
                "call_count": 0,
                "calls": [],
            }

        fc = self._costs[feature_id]
        return {
            "feature_id": feature_id,
            "total_input_tokens": fc.total_input_tokens,
            "total_output_tokens": fc.total_output_tokens,
            "total_cost_usd": round(fc.total_cost_usd, 6),
            "call_count": len(fc.calls),
            "calls": [asdict(c) for c in fc.calls],
        }

    def check_budget(self, feature_id: str) -> bool:
        """Check if feature is within budget.

        Args:
            feature_id: The feature identifier.

        Returns:
            True if within budget, False if budget exceeded.
        """
        if feature_id not in self._costs:
            return True
        return self._costs[feature_id].total_cost_usd < settings.per_feature_budget_usd

    async def reset_feature(self, feature_id: str) -> bool:
        """Reset costs for a feature.

        Args:
            feature_id: The feature identifier.

        Returns:
            True if feature existed and was reset, False otherwise.
        """
        async with self._lock:
            if feature_id not in self._costs:
                return False
            del self._costs[feature_id]
            await self._save()
            log.info("cost_tracker.reset_feature", feature_id=feature_id)
            return True

    def get_all_costs(self) -> dict[str, dict[str, Any]]:
        """Get all tracked costs.

        Returns:
            Dictionary mapping feature_id to cost summaries.
        """
        return {fid: self.get_feature_cost(fid) for fid in self._costs}

    async def record_manual(
        self,
        feature_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> None:
        """Manually record a cost entry (for non-LangChain calls).

        Args:
            feature_id: The feature identifier.
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.
            model: Model identifier.
        """
        cost = self._calculate_cost(model, input_tokens, output_tokens)
        record = CostRecord(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            model=model,
        )
        await self._add_record(feature_id, record)
        log.info(
            "cost_tracker.manual_record",
            feature_id=feature_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
