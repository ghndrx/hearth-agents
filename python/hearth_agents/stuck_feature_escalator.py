"""Escalate features stuck in ``implementing``.

The worker watchdog detects WORKERS that aren't beating. This task
detects FEATURES that have been in ``implementing`` (or ``researching``,
``reviewing``) far longer than ``per_feature_timeout_sec`` — long
enough that something stranded them. Typical causes: a crash mid-
ainvoke, a worker hang that bypassed the watchdog, a ``_claim_next``
bug leaving orphaned state.

Flips them to ``blocked`` with an explicit reason so the healer can
cycle them back, rather than letting them sit invisibly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .backlog import Backlog
from .config import settings
from .logger import log
from .transitions import read_tail

SCAN_INTERVAL_SEC = 5 * 60


def _sweep(backlog: Backlog) -> int:
    """One pass. Returns the count flipped."""
    # Stranded threshold: 3x per-feature timeout. Generous — we don't
    # want to race legitimate long runs.
    threshold_sec = max(60, settings.per_feature_timeout_sec * 3)
    now = datetime.now(timezone.utc).timestamp()
    # For each stuck-statuses feature, find the most recent transition
    # that moved it INTO that status. If older than threshold, flip.
    transitions = read_tail(limit=20000)
    last_into: dict[str, float] = {}
    for t in transitions:
        if (t.get("to") or "") in ("implementing", "researching", "reviewing"):
            fid = t.get("feature_id") or ""
            ts = t.get("ts") or ""
            try:
                epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            last_into[fid] = epoch  # chronological, last wins
    flipped = 0
    for f in backlog.features:
        if f.status not in ("implementing", "researching", "reviewing"):
            continue
        since = last_into.get(f.id)
        if since is None:
            continue
        age = now - since
        if age < threshold_sec:
            continue
        log.warning("feature_stranded", id=f.id, status=f.status, age_sec=int(age))
        backlog.set_status(
            f.id, "blocked",
            reason=f"stranded in {f.status} for {int(age)}s (threshold {threshold_sec}s)",
            actor="loop",
        )
        flipped += 1
    return flipped


async def run_stuck_feature_escalator(backlog: Backlog) -> None:
    """Background task. Runs every 5m; never raises externally."""
    from .heartbeat import beat
    await asyncio.sleep(SCAN_INTERVAL_SEC)
    while True:
        beat("stuck_feature_escalator")
        try:
            n = _sweep(backlog)
            if n:
                log.info("stuck_features_flipped", count=n)
        except Exception as e:  # noqa: BLE001
            log.warning("stuck_escalator_failed", err=str(e)[:200])
        await asyncio.sleep(SCAN_INTERVAL_SEC)
