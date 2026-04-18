"""Daily backlog archive task.

Runs `Backlog.archive_old_done(7)` once a day so the live backlog
doesn't grow unbounded. Done features remain reachable via the archive
file (`/data/archive.json`) but stop weighing on `/features` responses,
kanban rendering, and per-request transition lookups.
"""

from __future__ import annotations

import asyncio

from .backlog import Backlog
from .logger import log

ARCHIVE_INTERVAL_SEC = 24 * 60 * 60


async def run_archive(backlog: Backlog) -> None:
    """Background task: archive done features older than 7d, daily."""
    from .heartbeat import beat
    await asyncio.sleep(ARCHIVE_INTERVAL_SEC)
    while True:
        beat("archive")
        try:
            n = backlog.archive_old_done(max_age_days=7)
            if n:
                log.info("backlog_archived", count=n)
        except Exception as e:  # noqa: BLE001
            log.warning("backlog_archive_failed", err=str(e)[:200])
        await asyncio.sleep(ARCHIVE_INTERVAL_SEC)
