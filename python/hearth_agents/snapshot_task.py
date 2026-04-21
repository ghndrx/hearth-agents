"""Daily backlog snapshot task.

Copies ``backlog.json`` to ``/data/backlog-snapshots/YYYY-MM-DD.json``
once a day so we can time-machine: diff current against a snapshot to
see what moved, or restore a known-good state. Keeps the last 30 days;
older snapshots are deleted to cap disk.

Cheap — one file copy per day. Separate from the archive task (which
compacts old done features); this preserves the ENTIRE backlog shape
for operational forensics.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backlog import Backlog
from .logger import log

SNAPSHOT_INTERVAL_SEC = 24 * 60 * 60
# Retention window for daily snapshots. Overridable via
# SNAPSHOT_RETENTION_DAYS env so operators managing longer-retention
# audit requirements can extend without code change.
import os as _os
RETENTION_DAYS = int(_os.environ.get("SNAPSHOT_RETENTION_DAYS", "30"))


async def run_snapshot(backlog: Backlog) -> None:
    """Background task. Snapshots on boot, then every 24h; never raises
    externally.

    Boot-time snapshot matters more than it looks: if the container restart
    cycles faster than SNAPSHOT_INTERVAL_SEC (as happens during bursts of
    rapid deploys), the pre-sleep version of this loop would go many days
    without writing a snapshot. Then when backlog.json gets corrupted
    mid-write there's no recent restore point. Observed in prod: 2-day
    snapshot gap led to 113 features unrecoverable after a SIGKILL
    truncated backlog.json to 0 bytes.
    """
    from .heartbeat import beat
    beat("snapshot")
    # Boot snapshot is idempotent within a day (see _snapshot_once), so
    # restart storms don't multiply writes — they just keep today's file
    # fresh. Runs before the first sleep so we always have today's copy.
    try:
        _snapshot_once(backlog)
        _prune_old()
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot_boot_failed", err=str(e)[:200])
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
        beat("snapshot")
        try:
            _snapshot_once(backlog)
            _prune_old()
        except Exception as e:  # noqa: BLE001
            log.warning("snapshot_failed", err=str(e)[:200])


def _snapshot_once(backlog: Backlog) -> None:
    if backlog._path is None or not backlog._path.exists():
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = Path("/data/backlog-snapshots")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{today}.json"
    if dest.exists():
        return  # idempotent within the day
    shutil.copy2(backlog._path, dest)
    log.info("snapshot_written", path=str(dest), bytes=dest.stat().st_size)


def _prune_old() -> None:
    dest_dir = Path("/data/backlog-snapshots")
    if not dest_dir.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for p in dest_dir.glob("*.json"):
        try:
            day = datetime.strptime(p.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day < cutoff:
            try:
                p.unlink()
                log.info("snapshot_pruned", path=str(p))
            except OSError:
                pass
