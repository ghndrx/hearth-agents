"""Debugging-session replay analytics.

Research #3807 prescribes deterministic retrace of failed agent runs.
Full replay (re-running the agent against stored intermediate state)
needs Langfuse persistence or an independent recorder; what we ship
today is the READ-ONLY version: given a feature_id, return every
attempt we have in /data/attempts.jsonl grouped by prompts_version
plus a pairwise comparison of tool-call sequences across attempts.

Operators use this to answer:
  - "Did the agent try the same thing twice under the same prompts?"
  - "What changed in tool sequence between prompts_version A and B
    for the same feature?"
  - "How much token spend did each attempt cost?"
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .transitions import read_tail

_ATTEMPTS_PATH = Path("/data/attempts.jsonl")


def _load_attempts(feature_id: str) -> list[dict]:
    if not _ATTEMPTS_PATH.exists():
        return []
    out: list[dict] = []
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
                if e.get("feature_id") == feature_id:
                    out.append(e)
    except OSError:
        return []
    return out


def _load_versions_for_feature(feature_id: str) -> dict[str, list[dict]]:
    """For each transition recorded against this feature, map its
    prompts_version to the list of transitions under that version.
    Pairs with attempts_per_version so the caller can display "during
    prompts_version=X, here's what the agent ran."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for t in read_tail(limit=20000, feature_id=feature_id):
        v = t.get("prompts_version") or "unknown"
        grouped[v].append(t)
    return dict(grouped)


def replay(feature_id: str) -> dict[str, Any]:
    """Build the replay report for one feature."""
    attempts = _load_attempts(feature_id)
    transitions_by_ver = _load_versions_for_feature(feature_id)

    # Tool-call call sequences shown as a list of just names for compactness;
    # full args live in the raw attempts list for drill-down.
    def _seq(entry: dict) -> list[str]:
        return [(tc.get("name") or "?") for tc in (entry.get("tool_calls") or [])]

    # Diff consecutive attempts: show what's newly called, what's dropped.
    diffs: list[dict[str, Any]] = []
    for prev, curr in zip(attempts, attempts[1:]):
        prev_seq = _seq(prev)
        curr_seq = _seq(curr)
        added = [t for t in curr_seq if t not in prev_seq]
        dropped = [t for t in prev_seq if t not in curr_seq]
        diffs.append({
            "prev_attempt": prev.get("attempt"),
            "curr_attempt": curr.get("attempt"),
            "prev_provider": prev.get("provider"),
            "curr_provider": curr.get("provider"),
            "added_tools": sorted(set(added))[:20],
            "dropped_tools": sorted(set(dropped))[:20],
            "prev_tokens_in": prev.get("input_tokens"),
            "curr_tokens_in": curr.get("input_tokens"),
        })

    total_in = sum(int(a.get("input_tokens") or 0) for a in attempts)
    total_out = sum(int(a.get("output_tokens") or 0) for a in attempts)
    # Same pricing as the per-feature budget tracker.
    cost = (total_in / 1_000_000) * 0.30 + (total_out / 1_000_000) * 1.20

    return {
        "feature_id": feature_id,
        "attempts_count": len(attempts),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "estimated_cost_usd": round(cost, 4),
        "attempts": attempts,
        "pairwise_diffs": diffs,
        "transitions_by_version": transitions_by_ver,
    }
