"""Backlog drift canary.

Every 10m compares live Backlog state against the /backlog/replay
projection. Any drift (missing_in_live, missing_in_projection,
status_mismatches) triggers a coalesced Telegram alert — the point
is not to auto-repair (that's POST /backlog/repair), but to surface
divergence before it accumulates silently.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from .config import settings
from .heartbeat import beat
from .logger import log
from .notify import Notifier

CHECK_INTERVAL_SEC = 10 * 60


async def run_drift_canary() -> None:
    """Background task."""
    beat("drift_canary")
    notifier = Notifier()
    alerted_digest = ""  # last drift signature we alerted on; re-ping only when it changes
    try:
        while True:
            beat("drift_canary")
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                alerted_digest = await _check_once(notifier, alerted_digest)
            except Exception as e:  # noqa: BLE001
                log.warning("drift_canary_tick_failed", err=str(e)[:200])
    finally:
        await notifier.close()


async def _check_once(notifier: Notifier, prev_digest: str) -> str:
    url = f"http://127.0.0.1:{settings.server_port}/backlog/replay"
    try:
        def _get() -> dict:
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.loads(r.read())
        report = await asyncio.to_thread(_get)
    except Exception as e:  # noqa: BLE001
        log.warning("drift_canary_fetch_failed", err=str(e)[:200])
        return prev_digest
    if report.get("healthy"):
        return ""  # reset so re-divergence pings
    # Signature: combined counts so we don't spam on the same drift
    # tick after tick. Changing counts = still-moving drift = re-alert.
    digest = f"{report.get('projection_feature_count',0)}|{len(report.get('status_mismatches',[]))}|{len(report.get('missing_in_projection',[]))}|{len(report.get('missing_in_live',[]))}"
    if digest == prev_digest:
        return prev_digest
    mismatches = len(report.get("status_mismatches", []))
    missing_live = len(report.get("missing_in_live", []))
    missing_proj = len(report.get("missing_in_projection", []))
    # Auto-run the repair dry-run so the alert carries the proposed fix
    # inline — operator can decide whether to POST /backlog/repair with
    # force:true in one glance instead of an extra round-trip.
    preview_lines: list[str] = []
    try:
        def _post() -> dict:
            req = urllib.request.Request(
                f"http://127.0.0.1:{settings.server_port}/backlog/repair",
                data=json.dumps({"dry_run": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        preview = await asyncio.to_thread(_post)
        would_fix = preview.get("would_fix_status") or []
        would_remove = preview.get("would_remove") or []
        if would_fix:
            preview_lines.append(f"would fix {len(would_fix)} status:")
            for f in would_fix[:3]:
                preview_lines.append(f"  {f.get('id')}: {f.get('live')} → {f.get('projection')}")
        if would_remove:
            preview_lines.append(f"would remove {len(would_remove)} orphans (first: {would_remove[:3]})")
    except Exception as e:  # noqa: BLE001
        preview_lines.append(f"(dry-run probe failed: {str(e)[:80]})")
    msg = (
        f"⚠️ backlog drift detected\n"
        f"status_mismatches: {mismatches}\n"
        f"missing_in_live: {missing_live}  · missing_in_projection: {missing_proj}\n"
        + ("\n" + "\n".join(preview_lines) if preview_lines else "")
        + "\n\nCurl `/admin/replay-repair` with force:true to apply."
    )
    await notifier.send_coalesced("drift_canary", msg, min_interval_sec=3 * 3600)
    log.warning("drift_canary_fired", **{
        "mismatches": mismatches,
        "missing_in_live": missing_live,
        "missing_in_projection": missing_proj,
    })
    return digest
