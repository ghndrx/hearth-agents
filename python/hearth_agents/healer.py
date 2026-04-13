"""Auto-heal blocked features by flipping them back to pending.

A feature can end up ``blocked`` for many reasons — transient provider outage,
flaky test, timeout, an agent's bad mood. Instead of having humans resurrect
them by hand, the healer periodically resets blocked features to ``pending``
so the loop retries them.

Each reset increments ``feature.heal_attempts``; once the cap is reached the
feature stays ``blocked`` and a Telegram message asks the human to look. The
per-session fixup loop in ``loop.py`` handles tight in-run retries; the healer
handles longer-horizon reincarnation across worker cycles.
"""

from __future__ import annotations

import asyncio

from .backlog import Backlog
from .logger import log
from .notify import Notifier

HEAL_INTERVAL_SEC = 300  # scan every 5 minutes
HEAL_MAX_ATTEMPTS = 3    # after this many resets we stop and escalate
HEAL_COOLDOWN_SEC = 600  # skip features blocked less than this long — give in-run fixup a chance


async def run_healer(backlog: Backlog) -> None:
    """Background task: periodically resurrect blocked features."""
    notifier = Notifier()
    log.info("healer_started", interval_sec=HEAL_INTERVAL_SEC, max_attempts=HEAL_MAX_ATTEMPTS)
    try:
        while True:
            healed: list[str] = []
            escalated: list[str] = []
            for f in list(backlog.features):
                if f.status != "blocked":
                    continue
                if f.heal_attempts >= HEAL_MAX_ATTEMPTS:
                    # Only escalate once — track by marking a dummy status bump.
                    # For now we just log; human intervention is the ask.
                    continue
                # Reset to pending and bump counter. Persist via save() since
                # we're mutating the in-memory object directly.
                f.heal_attempts += 1
                f.status = "pending"
                healed.append(f"{f.id} (attempt {f.heal_attempts}/{HEAL_MAX_ATTEMPTS})")
                log.info("healer_reset", id=f.id, attempt=f.heal_attempts)

            # Identify features that just hit the ceiling for escalation.
            for f in backlog.features:
                if f.status == "blocked" and f.heal_attempts == HEAL_MAX_ATTEMPTS:
                    escalated.append(f.id)

            if healed:
                backlog.save()
                await notifier.send(f"🩹 healer reset {len(healed)} blocked: {', '.join(healed)[:300]}")
            if escalated:
                # Bump to prevent re-sending the same escalation every cycle.
                for f in backlog.features:
                    if f.id in escalated:
                        f.heal_attempts += 1  # >MAX_ATTEMPTS now, silences future escalations
                backlog.save()
                await notifier.send(
                    f"🚨 healer giving up on {len(escalated)} feature(s) after "
                    f"{HEAL_MAX_ATTEMPTS} attempts — human review needed: "
                    f"{', '.join(escalated)[:300]}"
                )

            await asyncio.sleep(HEAL_INTERVAL_SEC)
    finally:
        await notifier.close()
