"""Auto-seed self-improvement features from observed block patterns.

When a particular block reason crosses ``REASON_THRESHOLD`` occurrences
across the live backlog, queue a self-improvement Feature targeting it.
Closes the feedback loop that today requires me to re-prompt: the agent
now files its own bugs against itself when patterns get loud enough.

Idempotent — uses a stable feature_id derived from the reason hash so
the same reason doesn't seed multiple identical self-improvement
features.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter

from .backlog import Backlog, Feature
from .logger import log

SCAN_INTERVAL_SEC = 30 * 60      # twice an hour
REASON_THRESHOLD = 5              # need 5 features all blocked by same prefix
COOLDOWN_HOURS_PER_SEED = 24      # once per reason per day max


def _reason_id(reason_prefix: str) -> str:
    h = hashlib.sha256(reason_prefix.encode()).hexdigest()[:8]
    return f"self-improve-{h}"


def _scan(backlog: Backlog) -> int:
    """One pass. Returns count of self-improvement features seeded."""
    counter: Counter[str] = Counter()
    for f in backlog.features:
        if f.status != "blocked":
            continue
        prefix = (f.heal_hint or "(no hint)")[:80].strip().rstrip(":").rstrip(".")
        counter[prefix] += 1
    seeded = 0
    for prefix, count in counter.items():
        if count < REASON_THRESHOLD:
            continue
        fid = _reason_id(prefix)
        # Skip if already in backlog (stable id avoids reseed).
        if any(f.id == fid for f in backlog.features):
            continue
        feature = Feature(
            id=fid,
            name=f"Self-improvement: address recurring block ({count}× '{prefix[:40]}')",
            description=(
                f"Block reason '{prefix}' has appeared on {count} live features "
                f"under the current prompts_version. Investigate the agent's loop, "
                f"prompts, and tooling to reduce recurrence. Source: /prompt-analytics, "
                f"/repo-analytics, /data/transitions.jsonl. Acceptance: ship a code or "
                f"prompt change that demonstrably reduces this reason's count by 50% "
                f"over the next 24h."
            ),
            priority="high",
            repos=["hearth-agents"],
            self_improvement=True,
            kind="feature",
            acceptance_criteria=f"recurrence of '{prefix[:40]}' drops 50% within 24h",
        )
        if backlog.add(feature):
            seeded += 1
            log.info("self_improvement_seeded", reason_prefix=prefix[:60], count=count, feature_id=fid)
    return seeded


async def run_self_improvement_seeder(backlog: Backlog) -> None:
    """Background task. Idempotent + bounded by REASON_THRESHOLD."""
    from .heartbeat import beat
    beat("self_improvement_seeder")  # mark alive before initial sleep
    await asyncio.sleep(SCAN_INTERVAL_SEC)
    while True:
        beat("self_improvement_seeder")
        try:
            n = _scan(backlog)
            if n:
                log.info("self_improvement_seeder_seeded", count=n)
        except Exception as e:  # noqa: BLE001
            log.warning("self_improvement_seeder_failed", err=str(e)[:200])
        await asyncio.sleep(SCAN_INTERVAL_SEC)
