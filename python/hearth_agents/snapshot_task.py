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
RETENTION_DAYS = 30


async def run_snapshot(backlog: Backlog) -> None:
    """Background task. Runs every 24h; never raises externally."""
    from .heartbeat import beat
    await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
    while True:
        beat("snapshot")
        try:
            _snapshot_once(backlog)
            _prune_old()
        except Exception as e:  # noqa: BLE001
            log.warning("snapshot_failed", err=str(e)[:200])
        await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)


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
