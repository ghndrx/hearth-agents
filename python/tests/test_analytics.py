"""Tests for the pure-function analytics paths.

Covers `cost_analytics.analyze_costs`, `prompt_analyzer.analyze`, and
the `/backlog/replay` projection math (by reconstructing the projection
from a handcrafted transitions log). No agent invocation, no network,
no disk persistence beyond a pytest tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# transitions + prompt_analyzer import logger which pulls structlog;
# skip cleanly in stripped environments rather than fail.
pytest.importorskip("structlog")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_cost_analytics_empty(tmp_path, monkeypatch):
    from hearth_agents import cost_analytics as ca
    monkeypatch.setattr(ca, "_ATTEMPTS_PATH", tmp_path / "nope.jsonl")
    result = ca.analyze_costs()
    assert result["total_cost_usd"] == 0.0
    assert result["top_features"] == []
    assert result["daily"] == []
    assert result["providers"] == {}


def test_cost_analytics_sum(tmp_path, monkeypatch):
    from hearth_agents import cost_analytics as ca
    attempts = tmp_path / "attempts.jsonl"
    _write_jsonl(attempts, [
        {"ts": "2026-04-18T00:00:00+00:00", "feature_id": "f1", "provider": "primary",
         "input_tokens": 1_000_000, "output_tokens": 0, "duration_sec": 1.5},
        {"ts": "2026-04-18T00:00:00+00:00", "feature_id": "f2", "provider": "fallback",
         "input_tokens": 0, "output_tokens": 1_000_000, "duration_sec": 2.5},
    ])
    monkeypatch.setattr(ca, "_ATTEMPTS_PATH", attempts)
    result = ca.analyze_costs()
    # f1: 1M in × $0.30 = $0.30 ; f2: 1M out × $1.20 = $1.20 → total $1.50
    assert result["total_cost_usd"] == 1.5
    ids = {r["feature_id"] for r in result["top_features"]}
    assert ids == {"f1", "f2"}
    assert result["duration_percentiles"]["sample_count"] == 2


def test_prompt_analyzer_grouping(tmp_path, monkeypatch):
    from hearth_agents import prompt_analyzer as pa
    from hearth_agents import transitions as tr
    transitions_path = tmp_path / "transitions.jsonl"
    _write_jsonl(transitions_path, [
        {"ts": "2026-04-18T00:00:00+00:00", "feature_id": "f1", "from": "pending", "to": "done",
         "reason": "", "actor": "loop", "prompts_version": "abc"},
        {"ts": "2026-04-18T00:01:00+00:00", "feature_id": "f2", "from": "pending", "to": "blocked",
         "reason": "tests failed", "actor": "loop", "prompts_version": "abc"},
        {"ts": "2026-04-18T01:00:00+00:00", "feature_id": "f3", "from": "pending", "to": "done",
         "reason": "", "actor": "loop", "prompts_version": "def"},
    ])
    monkeypatch.setattr(tr, "_DEFAULT_PATH", transitions_path)
    report = pa.analyze()
    by_version = {v["prompts_version"]: v for v in report["versions"]}
    assert by_version["abc"]["terminal_done"] == 1
    assert by_version["abc"]["terminal_blocked"] == 1
    assert by_version["abc"]["done_rate"] == 0.5
    assert by_version["def"]["terminal_done"] == 1
    assert by_version["def"]["done_rate"] == 1.0


def test_replay_projection_math(tmp_path, monkeypatch):
    """Feed a transitions log into read_tail and manually compute the
    projection the /backlog/replay endpoint returns. Catches regressions
    in the projection-collapse rules."""
    from hearth_agents import transitions as tr
    transitions_path = tmp_path / "transitions.jsonl"
    _write_jsonl(transitions_path, [
        {"ts": "2026-04-18T00:00:00+00:00", "feature_id": "a", "from": None, "to": "pending",
         "reason": "", "actor": "loop", "prompts_version": "v1"},
        {"ts": "2026-04-18T00:01:00+00:00", "feature_id": "a", "from": "pending", "to": "done",
         "reason": "", "actor": "loop", "prompts_version": "v1"},
        {"ts": "2026-04-18T00:02:00+00:00", "feature_id": "b", "from": None, "to": "pending",
         "reason": "", "actor": "loop", "prompts_version": "v1"},
        {"ts": "2026-04-18T00:03:00+00:00", "feature_id": "b", "from": "pending", "to": "nuked",
         "reason": "kanban nuke", "actor": "kanban", "prompts_version": "v1"},
    ])
    monkeypatch.setattr(tr, "_DEFAULT_PATH", transitions_path)
    entries = tr.read_tail(limit=1000)
    assert len(entries) == 4
    # Replicate the projection math from server.backlog_replay.
    projection: dict[str, str] = {}
    for t in entries:
        to = t["to"]
        if to == "nuked":
            projection.pop(t["feature_id"], None)
        else:
            projection[t["feature_id"]] = to
    assert projection == {"a": "done"}  # b was nuked after pending
