"""Prompt-drift alarm.

Research #3825 (prompt-version A/B testing): once you have prompts_version
stamping, you can automatically detect when the active version regresses
vs recent history. This task runs every 30 minutes, reads
``prompt_analyzer.analyze()``, compares the CURRENT version's done-rate
(once it has enough samples) against the trailing median of recent
versions, and pings Telegram on a significant drop.

Conservative thresholds to avoid noise:
  - Current version needs >= 15 terminal transitions.
  - Regression threshold: current rate < trailing_median * 0.80.
  - One ping per version per boot (cleared on process restart).
"""

from __future__ import annotations

import asyncio
import statistics

from .logger import log
from .notify import Notifier
from .prompt_analyzer import analyze

DRIFT_INTERVAL_SEC = 30 * 60
MIN_SAMPLES_FOR_ACTIVE = 15
REGRESSION_RATIO = 0.80


async def run_drift_alarm() -> None:
    """Background task. Emits at most one alert per (version, boot)."""
    notifier = Notifier()
    alerted_versions: set[str] = set()
    await asyncio.sleep(DRIFT_INTERVAL_SEC)
    try:
        while True:
            try:
                _check_and_alert(alerted_versions, notifier)
            except Exception as e:  # noqa: BLE001
                log.warning("drift_check_failed", err=str(e)[:200])
            await asyncio.sleep(DRIFT_INTERVAL_SEC)
    finally:
        await notifier.close()


def _check_and_alert(alerted_versions: set[str], notifier: Notifier) -> None:
    report = analyze()
    versions = report.get("versions", [])
    if not versions:
        return
    active = versions[0]
    active_id = active.get("prompts_version")
    if not active_id or active_id in alerted_versions:
        return
    total = active.get("terminal_done", 0) + active.get("terminal_blocked", 0)
    if total < MIN_SAMPLES_FOR_ACTIVE:
        return
    prior = [
        v.get("done_rate", 0.0)
        for v in versions[1:]
        if not v.get("low_confidence") and v.get("prompts_version") != active_id
    ]
    if len(prior) < 2:
        return
    trailing_median = statistics.median(prior)
    if trailing_median <= 0:
        return
    current = active.get("done_rate", 0.0)
    ratio = current / trailing_median
    if ratio >= REGRESSION_RATIO:
        return
    alerted_versions.add(active_id)
    msg = (
        f"⚠️ prompt_drift_detected\n"
        f"Active version {active_id}: done_rate={current:.1%} "
        f"over {total} terminal transitions.\n"
        f"Trailing median of prior trusted versions: {trailing_median:.1%}.\n"
        f"Ratio {ratio:.2f} below threshold {REGRESSION_RATIO:.2f}. "
        f"Consider rollback or audit recent prompt changes."
    )
    log.warning(
        "prompt_drift_detected",
        active=active_id,
        current_rate=round(current, 3),
        trailing_median=round(trailing_median, 3),
        samples=total,
    )
    asyncio.create_task(notifier.send(msg))
