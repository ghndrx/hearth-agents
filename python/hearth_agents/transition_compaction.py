"""Monthly transition-log rotation.

``transitions.jsonl`` is append-only by design (audit trail) and grows
unboundedly. Over months that file will bloat enough to slow
``read_tail`` and inflate ``/transitions`` payloads. This task rotates
entries older than ~60 days into
``/data/transitions-archive-YYYY-MM.jsonl`` once a day.

The live file stays small enough for interactive reads; archives are
still on disk for deep forensics. read_tail only reads the live file
by default — archives are explicitly opt-in via read_archive(month).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .heartbeat import beat
from .logger import log

COMPACTION_INTERVAL_SEC = 24 * 60 * 60
LIVE_RETENTION_DAYS = 60


def _compact_once() -> int:
    """One pass. Returns count of entries archived off the live file."""
    live_path = Path("/data/transitions.jsonl")
    if not live_path.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=LIVE_RETENTION_DAYS)
    cutoff_ts = cutoff.timestamp()
    keep_lines: list[str] = []
    archive_groups: dict[str, list[str]] = defaultdict(list)
    archived = 0
    try:
        with live_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                # Parse ts lazily to avoid full-JSON cost on every line.
                try:
                    entry = json.loads(stripped)
                    ts_iso = entry.get("ts", "")
                    ts_epoch = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
                except (ValueError, json.JSONDecodeError):
                    keep_lines.append(stripped)
                    continue
                if ts_epoch >= cutoff_ts:
                    keep_lines.append(stripped)
                else:
                    month_key = ts_iso[:7]  # YYYY-MM
                    archive_groups[month_key].append(stripped)
                    archived += 1
    except OSError as e:
        log.warning("compaction_read_failed", err=str(e)[:200])
        return 0
    if not archived:
        return 0
    # Append archives per month file. Multiple passes stay correct.
    for month_key, lines in archive_groups.items():
        archive_path = Path(f"/data/transitions-archive-{month_key}.jsonl")
        try:
            with archive_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            log.warning("compaction_archive_write_failed", month=month_key, err=str(e)[:200])
            return 0
    # Rewrite the live file atomically via tmp + rename.
    tmp_path = live_path.with_suffix(".jsonl.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(keep_lines) + ("\n" if keep_lines else ""))
        tmp_path.replace(live_path)
    except OSError as e:
        log.warning("compaction_rewrite_failed", err=str(e)[:200])
        return 0
    log.info("transitions_compacted", archived=archived, live_remaining=len(keep_lines))
    return archived


async def run_transition_compaction() -> None:
    """Background task; runs daily."""
    beat("transition_compaction")
    await asyncio.sleep(COMPACTION_INTERVAL_SEC)
    while True:
        beat("transition_compaction")
        try:
            _compact_once()
        except Exception as e:  # noqa: BLE001
            log.warning("transition_compaction_tick_failed", err=str(e)[:200])
        await asyncio.sleep(COMPACTION_INTERVAL_SEC)
