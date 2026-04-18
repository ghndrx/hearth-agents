"""Cron-style scheduled feature enqueue.

Reads ``/data/schedule.yaml`` (or JSON) on startup and every 60s
thereafter. Each entry is a spec that auto-creates a Feature at a
defined cadence — weekly dep audits, daily deploy-drift checks,
monthly security scans, etc. The operator doesn't hand-queue recurring
work; it just shows up on the backlog when it's time.

Schedule file format (YAML-ish; JSON works too):

  - name: weekly dep audit
    every_hours: 168      # once a week
    feature:
      id_prefix: weekly-dep-audit
      name: "Weekly dependency audit"
      description: "Run npm audit / govulncheck / pip-audit across repos..."
      kind: security
      priority: medium
      repos: [hearth, hearth-desktop, hearth-mobile]

The ``id_prefix`` is suffixed with a timestamp so each scheduled run
creates a unique feature_id (no dedup collision with a prior run).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .backlog import Backlog, Feature
from .logger import log

SCHEDULE_PATH = Path("/data/schedule.json")
SCAN_INTERVAL_SEC = 60


@dataclass
class ScheduleEntry:
    name: str
    every_hours: float
    last_fire_ts: float  # epoch seconds
    feature_spec: dict


def _load_entries() -> list[ScheduleEntry]:
    if not SCHEDULE_PATH.exists():
        return []
    try:
        raw = json.loads(SCHEDULE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("schedule_load_failed", err=str(e)[:200])
        return []
    if not isinstance(raw, list):
        return []
    out: list[ScheduleEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        every = float(item.get("every_hours") or 0)
        spec = item.get("feature") or {}
        last_fire = float(item.get("last_fire_ts") or 0)
        if name and every > 0 and spec.get("id_prefix"):
            out.append(ScheduleEntry(name=name, every_hours=every, last_fire_ts=last_fire, feature_spec=spec))
    return out


def _save_entries(entries: list[ScheduleEntry]) -> None:
    try:
        SCHEDULE_PATH.write_text(json.dumps(
            [
                {
                    "name": e.name,
                    "every_hours": e.every_hours,
                    "last_fire_ts": e.last_fire_ts,
                    "feature": e.feature_spec,
                }
                for e in entries
            ],
            indent=2,
        ))
    except OSError as e:
        log.warning("schedule_save_failed", err=str(e)[:200])


def _fire(entry: ScheduleEntry, backlog: Backlog) -> bool:
    """Enqueue one run of the scheduled spec. Returns True on success."""
    spec = entry.feature_spec
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M")
    fid = f"{spec['id_prefix']}-{stamp}"[:60]
    feature = Feature(
        id=fid,
        name=spec.get("name", entry.name),
        description=spec.get("description", "(scheduled)"),
        priority=spec.get("priority", "medium"),  # type: ignore[arg-type]
        repos=spec.get("repos", ["hearth"]),  # type: ignore[arg-type]
        kind=spec.get("kind", "feature"),  # type: ignore[arg-type]
        acceptance_criteria=spec.get("acceptance_criteria", ""),
    )
    added = backlog.add(feature)
    if added:
        log.info("scheduled_feature_enqueued", name=entry.name, feature_id=fid)
    return added


async def run_scheduler(backlog: Backlog) -> None:  # noqa: D401
    """Background task. Re-reads the schedule file each tick so operators
    can edit it live without a restart."""
    from .heartbeat import beat
    while True:
        beat("scheduler")
        try:
            entries = _load_entries()
            import time as _t
            now = _t.time()
            dirty = False
            for e in entries:
                due = e.last_fire_ts == 0 or (now - e.last_fire_ts) >= e.every_hours * 3600
                if not due:
                    continue
                if _fire(e, backlog):
                    e.last_fire_ts = now
                    dirty = True
            if dirty:
                _save_entries(entries)
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler_tick_failed", err=str(exc)[:200])
        await asyncio.sleep(SCAN_INTERVAL_SEC)
