"""Process-wide subsystem heartbeat registry.

Background tasks call ``beat("name")`` once per loop iteration. The
``/health`` endpoint reads the registry and reports liveness per
subsystem so an outage in one task (healer wedged, scheduler crashed,
drift_alarm exception loop) is visible without log-grepping.

Stale threshold per subsystem is its own scan interval × 3 — generous
to avoid false positives while still catching genuine wedge cases.
"""

from __future__ import annotations

import time

# Per-subsystem last-beat timestamp.
_beats: dict[str, float] = {}

# Default expected interval in seconds; overridden per subsystem.
DEFAULT_INTERVAL_SEC = 5 * 60

# Per-subsystem expected interval. A subsystem is "stale" if last beat
# is older than 3× its interval.
_intervals: dict[str, int] = {
    "healer": 300,
    "idea_engine": 1800,
    "worktree_gc": 1800,
    "digest": 86400,
    "drift_alarm": 1800,
    "archive": 86400,
    "scheduler": 60,
    "stuck_feature_escalator": 300,
    "self_improvement_seeder": 1800,
    "snapshot": 86400,
    "watchdog": 60,
    "autoscaler": 60,
}


def beat(subsystem: str) -> None:
    """Mark a subsystem alive at this moment."""
    _beats[subsystem] = time.time()


def status() -> dict[str, dict[str, float | bool | int]]:
    """Snapshot for /health. For every registered interval, report
    {age_sec, stale, expected_interval_sec}."""
    now = time.time()
    out: dict[str, dict[str, float | bool | int]] = {}
    for name, interval in _intervals.items():
        last = _beats.get(name)
        if last is None:
            out[name] = {"age_sec": -1, "stale": True, "expected_interval_sec": interval}
        else:
            age = now - last
            out[name] = {
                "age_sec": int(age),
                "stale": age > interval * 3,
                "expected_interval_sec": interval,
            }
    return out
