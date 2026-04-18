"""Daily budget alarm.

Every hour, compares today's cumulative spend (from /cost-analytics.daily)
against ``settings.daily_budget_usd``. Fires a coalesced Telegram alert
(once per 6h to avoid spam) when spend exceeds the threshold. Doesn't
stop the loop — operators decide whether to flip read-only.

Separate from per-feature budget cap (which throttles individual
features) — this is the aggregate-per-day guardrail.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from datetime import datetime, timezone

from .config import settings
from .heartbeat import beat
from .logger import log
from .notify import Notifier

CHECK_INTERVAL_SEC = 60 * 60  # hourly


async def run_budget_alarm() -> None:
    """Background task."""
    beat("budget_alarm")
    notifier = Notifier()
    try:
        while True:
            beat("budget_alarm")
            try:
                await _check_once(notifier)
            except Exception as e:  # noqa: BLE001
                log.warning("budget_alarm_tick_failed", err=str(e)[:200])
            await asyncio.sleep(CHECK_INTERVAL_SEC)
    finally:
        await notifier.close()


async def _check_once(notifier: Notifier) -> None:
    if settings.daily_budget_usd <= 0:
        return  # disabled
    # Pull today's spend via local HTTP.
    url = f"http://127.0.0.1:{settings.server_port}/cost-analytics"
    try:
        def _get() -> dict:
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.loads(r.read())
        data = await asyncio.to_thread(_get)
    except Exception as e:  # noqa: BLE001
        log.warning("budget_alarm_fetch_failed", err=str(e)[:200])
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_entry = next(
        (d for d in (data.get("daily") or []) if d.get("day") == today),
        None,
    )
    if today_entry is None:
        return
    spent = float(today_entry.get("cost_usd") or 0.0)
    if spent < settings.daily_budget_usd:
        return
    pct = (spent / settings.daily_budget_usd) * 100
    msg = (
        f"💸 daily budget alarm\n"
        f"today spend: ${spent:.2f} of ${settings.daily_budget_usd:.2f} ({pct:.0f}%)\n"
        f"consider toggling read-only mode: /admin/read-only {{\"enabled\":true}}"
    )
    sent = await notifier.send_coalesced(
        "budget_alarm",
        msg,
        min_interval_sec=6 * 3600,  # max one ping per 6h
    )
    if sent:
        log.warning("budget_alarm_fired", spent=round(spent, 3), budget=settings.daily_budget_usd)
