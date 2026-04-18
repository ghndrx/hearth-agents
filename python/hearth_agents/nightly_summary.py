"""Nightly consolidated summary.

Runs once per ~24h cycle. Combines signals operators care about on a
daily cadence into one Telegram message:
  - 24h done / blocked counts (from /stats)
  - End-of-month spend forecast
  - Current prompts_version + done-rate vs trailing median
  - Top 3 block reasons
  - Any drift alerts that would fire
  - Snapshot diff vs 24h ago (if snapshots exist)

Separate from the existing digest task — digest is just throughput
counts; this one is the synthesis operators actually want to read with
coffee. Scheduler-friendly: fires at NIGHTLY_HOUR_UTC (default 9:00
UTC = morning PT).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .backlog import Backlog
from .heartbeat import beat
from .logger import log
from .notify import Notifier
from .prompt_analyzer import analyze as analyze_prompts
from .transitions import read_tail


NIGHTLY_HOUR_UTC = 9  # operator morning window


def _format_message(backlog: Backlog) -> str:
    """Build one human-readable summary block."""
    import urllib.request
    import json as _json
    from .config import settings
    stats = backlog.stats()

    # 24h transitions
    recent_done = recent_blocked = 0
    day_ago = datetime.now(timezone.utc).timestamp() - 86400
    for t in read_tail(limit=20000):
        try:
            ts = datetime.fromisoformat((t.get("ts") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts < day_ago:
            continue
        if t.get("to") == "done":
            recent_done += 1
        elif t.get("to") == "blocked":
            recent_blocked += 1

    # Cost forecast via local HTTP
    forecast_line = ""
    try:
        url = f"http://127.0.0.1:{settings.server_port}/cost-analytics/forecast"
        with urllib.request.urlopen(url, timeout=5) as r:
            f = _json.loads(r.read())
        forecast_line = (
            f"💰 spend MTD ${f.get('month_to_date_usd', 0):.2f} · "
            f"forecast eom ${f.get('forecast_usd', 0):.2f}"
        )
    except Exception:  # noqa: BLE001
        forecast_line = "💰 cost: unavailable"

    # Prompt analytics
    pa = analyze_prompts()
    active = pa.get("versions", [{}])[0] if pa.get("versions") else {}
    active_rate = active.get("done_rate", 0.0)
    best_rate = pa.get("best_trusted_done_rate") or 0.0

    # Top block reasons
    from collections import Counter
    reason_counter: Counter[str] = Counter()
    for f in backlog.features:
        if f.status != "blocked":
            continue
        prefix = (f.heal_hint or "(no hint)")[:60].strip().rstrip(":").rstrip(".")
        reason_counter[prefix] += 1

    lines = [
        "🌅 *Nightly summary*",
        f"Backlog: done {stats.get('done', 0)} · blocked {stats.get('blocked', 0)} · "
        f"pending {stats.get('pending', 0)} · implementing {stats.get('implementing', 0)}",
        f"24h: +{recent_done} done / +{recent_blocked} blocked",
        forecast_line,
        f"Active prompts: `{active.get('prompts_version','?')}` @ {active_rate:.1%}"
        + (f" (best trusted {best_rate:.1%})" if best_rate else ""),
    ]
    if reason_counter:
        lines.append("Top blocks:")
        for r, c in reason_counter.most_common(3):
            lines.append(f"  {c}× {r[:55]}")
    return "\n".join(lines)


async def _sleep_until_next_fire() -> None:
    """Sleep until the next NIGHTLY_HOUR_UTC boundary."""
    now = datetime.now(timezone.utc)
    fire = now.replace(hour=NIGHTLY_HOUR_UTC, minute=0, second=0, microsecond=0)
    if fire <= now:
        fire = fire.replace(day=fire.day + 1) if fire.day < 28 else fire.replace(day=1, month=fire.month % 12 + 1)
    seconds = max(60.0, (fire - now).total_seconds())
    await asyncio.sleep(seconds)


async def run_nightly_summary(backlog: Backlog) -> None:
    """Background task; emits once per 24h at NIGHTLY_HOUR_UTC."""
    beat("nightly_summary")
    notifier = Notifier()
    try:
        while True:
            beat("nightly_summary")
            await _sleep_until_next_fire()
            beat("nightly_summary")
            try:
                msg = _format_message(backlog)
                await notifier.send(msg)
                log.info("nightly_summary_sent", length=len(msg))
            except Exception as e:  # noqa: BLE001
                log.warning("nightly_summary_failed", err=str(e)[:200])
    finally:
        await notifier.close()
