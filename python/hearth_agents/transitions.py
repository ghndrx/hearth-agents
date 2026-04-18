"""Append-only transition log for feature status changes.

Each status change writes one JSON line to ``/data/transitions.jsonl`` so
operators can answer "why did feature X move to blocked at 14:07?" by
grepping this file or tailing it live. Also the seed for future
event-sourcing of the kanban board (research #3802).

Lines are never mutated or deleted in place — this file is the audit
trail, not a cache. If disk becomes a concern, log-rotate externally.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .logger import log

_DEFAULT_PATH = Path(os.environ.get("TRANSITIONS_PATH", "/data/transitions.jsonl"))


@lru_cache(maxsize=1)
def prompts_version() -> str:
    """Combined sha of every file containing prompt content the agent
    actually sees: prompts.py (orchestrator/subagent text) + loop.py
    (per-feature prompt builder, fixup prompts, breaking-change prompt).
    Cached per-process — a restart rolls to the new hash, which is the
    boundary we care about for A/B attribution.

    Earlier versions only hashed prompts.py, missing loop.py edits that
    reshape the agent's actual input. This wider hash makes
    /prompt-analytics's done-rate attribution honest."""
    sources: list[bytes] = []
    here = Path(__file__).parent
    for name in ("prompts.py", "loop.py"):
        try:
            sources.append(here.joinpath(name).read_bytes())
        except OSError:
            sources.append(b"")
    if not any(sources):
        return "unknown"
    h = hashlib.sha256()
    for s in sources:
        h.update(s)
        h.update(b"\x1e")  # separator so concatenation is unambiguous
    return h.hexdigest()[:10]


def read_tail(limit: int = 500, feature_id: str | None = None) -> list[dict]:
    """Read the last ``limit`` transition entries (optionally filtered to one
    feature). Reads the whole file — fine up to ~tens of thousands of entries,
    then we should switch to a reverse-line iterator. Empty list when the
    file doesn't exist yet (no transitions recorded)."""
    if not _DEFAULT_PATH.exists():
        return []
    try:
        with _DEFAULT_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("transition_log_read_failed", err=str(e)[:200])
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if feature_id and entry.get("feature_id") != feature_id:
            continue
        out.append(entry)
    return out[-limit:]


def record_transition(
    feature_id: str,
    from_status: str | None,
    to_status: str,
    reason: str = "",
    actor: str = "loop",
) -> None:
    """Append one transition line. Never raises — a failed write just logs
    a warning and swallows, so a wedged disk can't take down the loop.

    ``actor`` distinguishes ``loop`` (auto), ``healer`` (resurrection),
    ``kanban`` (human via UI), and ``webhook`` (GitHub) so the history
    can be filtered by origin.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_id": feature_id,
        "from": from_status,
        "to": to_status,
        "reason": reason[:500],  # cap so a giant stack trace doesn't bloat lines
        "actor": actor,
        "prompts_version": prompts_version(),
    }
    try:
        _DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEFAULT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("transition_log_write_failed", err=str(e)[:200], feature=feature_id)

    # Fan out to the outbound webhook with at-least-once delivery via a
    # small in-memory retry queue. A dropped subscriber doesn't lose
    # events across the process lifetime (process restart still loses
    # the queue; for durable delivery, the subscriber should persist).
    try:
        from .config import settings
        if settings.outbound_transition_webhook_url:
            _enqueue_outbound_webhook(entry)
    except Exception:  # noqa: BLE001
        pass


# Outbound-webhook retry queue. In-memory; lost on restart. Background
# pump delivers with exponential backoff up to _MAX_ATTEMPTS.
_outbound_queue: list[tuple[int, dict]] = []  # (attempt_count, entry)
_outbound_lock_started = False
_MAX_ATTEMPTS = 5


def _enqueue_outbound_webhook(entry: dict) -> None:
    _outbound_queue.append((0, dict(entry)))
    # Lazily spawn the pump on first enqueue.
    global _outbound_lock_started
    if _outbound_lock_started:
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(_outbound_pump())
        _outbound_lock_started = True
    except RuntimeError:
        pass


async def _outbound_pump() -> None:
    """Drain the retry queue with backoff. One pump task per process."""
    import asyncio
    import httpx
    from .config import settings
    while True:
        if not _outbound_queue:
            await asyncio.sleep(5)
            continue
        attempts, entry = _outbound_queue.pop(0)
        url = settings.outbound_transition_webhook_url
        if not url:
            continue  # config flipped off; drop silently
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json=entry)
            if 200 <= r.status_code < 300:
                continue  # delivered
            raise httpx.HTTPError(f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            next_attempt = attempts + 1
            if next_attempt >= _MAX_ATTEMPTS:
                log.warning(
                    "outbound_webhook_dropped",
                    feature=entry.get("feature_id"),
                    attempts=next_attempt,
                    err=str(e)[:160],
                )
                continue
            # Exponential backoff: 5s, 10s, 20s, 40s.
            delay = 5 * (2 ** attempts)
            log.info("outbound_webhook_retry", attempts=next_attempt, delay_sec=delay)
            await asyncio.sleep(delay)
            _outbound_queue.append((next_attempt, entry))
