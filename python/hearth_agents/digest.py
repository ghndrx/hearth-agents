"""Daily Telegram digest of agent activity.

Per-feature pings are batched by the healer. Per-block escalations are
coalesced. What was missing: a rollup the operator can actually skim
over morning coffee — "what did the agent do yesterday?".

This task fires once every 24h (from first boot), posts the digest via
the ``Notifier``, and then sleeps. A restart re-aligns timing from
process start; exact calendar-day boundaries aren't needed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .backlog import Backlog
from .logger import log
from .notify import Notifier
from .transitions import read_tail

DIGEST_INTERVAL_SEC = 24 * 60 * 60  # 24h rollup


async def run_digest(backlog: Backlog) -> None:
    """Background task: emit a 24h summary to Telegram daily."""
    from .heartbeat import beat
    notifier = Notifier()
    # Wait one interval before the first digest so a restart storm doesn't
    # ping multiple times. The operator can always hit /stats for on-demand.
    await asyncio.sleep(DIGEST_INTERVAL_SEC)
    try:
        while True:
            beat("digest")
            try:
                msg = _compose_digest(backlog)
                await notifier.send(msg)
                log.info("digest_sent", length=len(msg))
            except Exception as e:  # noqa: BLE001
                log.warning("digest_compose_failed", err=str(e)[:200])
            await asyncio.sleep(DIGEST_INTERVAL_SEC)
    finally:
        await notifier.close()


def _compose_digest(backlog: Backlog) -> str:
    """Format a compact Telegram-friendly digest. Walks transitions.jsonl
    for yesterday-only activity + current backlog shape."""
    now = datetime.now(timezone.utc)
    since = now.timestamp() - DIGEST_INTERVAL_SEC
    transitions = read_tail(limit=50000)
    recent = [
        t for t in transitions
        if _to_ts(t.get("ts", "")) >= since
    ]
    done = sum(1 for t in recent if t.get("to") == "done")
    blocked = sum(1 for t in recent if t.get("to") == "blocked")
    healed = sum(1 for t in recent if t.get("to") == "pending" and t.get("actor") == "healer")
    approved = sum(1 for t in recent if t.get("to") == "done" and t.get("actor") == "kanban")
    nuked = sum(1 for t in recent if t.get("to") == "nuked")
    stats = backlog.stats()
    blocked_total = stats.get("blocked", 0)
    pending = stats.get("pending", 0)
    impl = stats.get("implementing", 0) + stats.get("researching", 0) + stats.get("reviewing", 0)
    return (
        "📊 hearth-agents 24h digest\n"
        f"\n*Activity:*\n"
        f"  ✅ done: {done}\n"
        f"  ❌ blocked: {blocked}\n"
        f"  🩹 healed: {healed}\n"
        f"  👤 kanban approved: {approved}\n"
        f"  🗑 nuked: {nuked}\n"
        f"\n*Current backlog:*\n"
        f"  pending: {pending}  ·  implementing: {impl}\n"
        f"  blocked: {blocked_total}  ·  done total: {stats.get('done', 0)}\n"
        f"\nTotal features: {stats.get('total', 0)}"
    )


def _to_ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
