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
from .verify import verify_changes

HEAL_INTERVAL_SEC = 300  # scan every 5 minutes
HEAL_MAX_ATTEMPTS = 3    # after this many resets we stop and escalate
HEAL_COOLDOWN_SEC = 600  # skip features blocked less than this long — give in-run fixup a chance


def _hint_for_reason(reason: str) -> str:
    """Translate a verify_changes reason into a targeted instruction the
    next-attempt prompt can paste in. Empty string when we have no specific
    advice (the agent then runs with the normal prompt).
    """
    r = reason.lower()
    if "no commits" in r:
        return (
            "PRIOR FAILURE: you opened a worktree last time and never committed "
            "anything. Do NOT enter another exploratory read-only spiral. After "
            "at most 6 reads, start writing with edit_file/write_file. End the "
            "session with either (a) a real git_commit on the feature branch or "
            "(b) a single message saying 'BLOCKED: <one concrete blocker>' and "
            "no commit. Both are acceptable; abandoning silently is not."
        )
    if "diff too large" in r:
        return (
            "PRIOR FAILURE: your diff exceeded the 600-line cap. This time, "
            "implement only the minimum viable slice that satisfies the feature "
            "name; defer secondary concerns to follow-up features. Target "
            "<300 lines of diff. If the feature genuinely cannot be sliced, "
            "report 'BLOCKED: needs decomposition' rather than over-shipping."
        )
    if "planner_undercount" in r:
        return (
            "PRIOR FAILURE: actual diff exceeded planner's estimate by >1.5x. "
            "The planner under-estimated the scope. This time, have the planner "
            "either (a) raise estimated_diff_lines to a realistic value, or "
            "(b) split the feature into per-concern sub-features BEFORE delegating. "
            "Do not re-run the same plan hoping for a smaller diff."
        )
    if "tests failed" in r:
        return (
            "PRIOR FAILURE: the test suite failed last attempt. Read the failing "
            "test output FIRST (run_command the test command, parse the failure), "
            "fix only what's broken, re-run the same test, then run the full suite."
        )
    if "never pushed" in r:
        return (
            "PRIOR FAILURE: you committed locally but never pushed. End with a "
            "git push -u origin HEAD and verify with git ls-remote --heads origin."
        )
    return ""


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
                # Re-verify to learn WHY this feature is blocked, so the next
                # attempt's prompt can carry a targeted hint instead of just
                # "try again". Without this the agent typically repeats the
                # exact failure mode (the 7/9 'no commits' cluster we saw).
                try:
                    _, reason = verify_changes(f)
                except Exception as e:  # noqa: BLE001
                    reason = f"healer could not re-verify: {e}"
                f.heal_hint = _hint_for_reason(reason)
                f.heal_attempts += 1
                f.status = "pending"
                healed.append(f"{f.id} (attempt {f.heal_attempts}/{HEAL_MAX_ATTEMPTS})")
                log.info("healer_reset", id=f.id, attempt=f.heal_attempts, reason=reason[:120])

            # Identify features that just hit the ceiling for escalation.
            for f in backlog.features:
                if f.status == "blocked" and f.heal_attempts == HEAL_MAX_ATTEMPTS:
                    escalated.append(f.id)

            if healed:
                backlog.save()
                # Coalesce healer batch notifications — with 150+ blocked
                # features the loop was firing this every 5 min. An hourly
                # summary is enough signal; raw log keeps per-feature detail.
                await notifier.send_coalesced(
                    "healer_batch",
                    f"🩹 healer reset {len(healed)} blocked this cycle "
                    "(further reset batches suppressed for 1h)",
                )
            if escalated:
                # Bump to prevent re-sending the same escalation every cycle.
                for f in backlog.features:
                    if f.id in escalated:
                        f.heal_attempts += 1  # >MAX_ATTEMPTS now, silences future escalations
                backlog.save()
                # Escalations ARE user-actionable — keep the raw send but still
                # coalesce so 50 escalations don't become 50 pings.
                await notifier.send_coalesced(
                    "healer_escalation",
                    f"🚨 healer giving up on {len(escalated)} feature(s) — human review needed",
                )

            await asyncio.sleep(HEAL_INTERVAL_SEC)
    finally:
        await notifier.close()
