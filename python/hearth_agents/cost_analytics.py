"""Per-feature + daily cost analytics from /data/attempts.jsonl.

Turns the raw token-usage log into the aggregate views operators
actually want: which features cost the most, what's the daily spend
trend, and which provider burned what.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_ATTEMPTS_PATH = Path("/data/attempts.jsonl")

_PRICE_IN_PER_1M = 0.30
_PRICE_OUT_PER_1M = 1.20


def _cost(in_tokens: int, out_tokens: int) -> float:
    return (in_tokens / 1_000_000) * _PRICE_IN_PER_1M + (out_tokens / 1_000_000) * _PRICE_OUT_PER_1M


def analyze_costs() -> dict[str, Any]:
    """Return per-feature totals + daily series + per-provider split."""
    if not _ATTEMPTS_PATH.exists():
        return {
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "top_features": [],
            "daily": [],
            "providers": {},
        }
    per_feature_in: dict[str, int] = defaultdict(int)
    per_feature_out: dict[str, int] = defaultdict(int)
    per_feature_attempts: dict[str, int] = defaultdict(int)
    per_day_in: dict[str, int] = defaultdict(int)
    per_day_out: dict[str, int] = defaultdict(int)
    per_provider_in: dict[str, int] = defaultdict(int)
    per_provider_out: dict[str, int] = defaultdict(int)
    total_in = total_out = 0
    try:
        with _ATTEMPTS_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fid = e.get("feature_id") or ""
                tin = int(e.get("input_tokens") or 0)
                tout = int(e.get("output_tokens") or 0)
                per_feature_in[fid] += tin
                per_feature_out[fid] += tout
                per_feature_attempts[fid] += 1
                day = (e.get("ts") or "")[:10]  # YYYY-MM-DD
                if day:
                    per_day_in[day] += tin
                    per_day_out[day] += tout
                prov = e.get("provider") or "?"
                per_provider_in[prov] += tin
                per_provider_out[prov] += tout
                total_in += tin
                total_out += tout
    except OSError:
        pass

    features = sorted(
        (
            {
                "feature_id": fid,
                "attempts": per_feature_attempts[fid],
                "input_tokens": per_feature_in[fid],
                "output_tokens": per_feature_out[fid],
                "cost_usd": round(_cost(per_feature_in[fid], per_feature_out[fid]), 4),
            }
            for fid in per_feature_in
        ),
        key=lambda d: -d["cost_usd"],
    )

    daily = sorted(
        (
            {
                "day": day,
                "input_tokens": per_day_in[day],
                "output_tokens": per_day_out[day],
                "cost_usd": round(_cost(per_day_in[day], per_day_out[day]), 4),
            }
            for day in per_day_in
        ),
        key=lambda d: d["day"],
    )

    providers = {
        p: {
            "input_tokens": per_provider_in[p],
            "output_tokens": per_provider_out[p],
            "cost_usd": round(_cost(per_provider_in[p], per_provider_out[p]), 4),
        }
        for p in set(list(per_provider_in) + list(per_provider_out))
    }

    return {
        "total_cost_usd": round(_cost(total_in, total_out), 4),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "top_features": features[:25],
        "daily": daily[-30:],  # last 30 days
        "providers": providers,
    }
