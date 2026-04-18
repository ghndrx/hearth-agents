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

    # Fan out to the outbound webhook (if configured). Fire-and-forget;
    # a slow or failing subscriber can't wedge the loop. Import lazily to
    # keep transitions.py free of config + httpx during tests.
    try:
        from .config import settings
        if settings.outbound_transition_webhook_url:
            import asyncio
            import httpx
            async def _post() -> None:
                try:
                    async with httpx.AsyncClient(timeout=5) as c:
                        await c.post(settings.outbound_transition_webhook_url, json=entry)
                except httpx.HTTPError as e:
                    log.warning("outbound_webhook_failed", err=str(e)[:160])
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_post())
            except RuntimeError:
                # No running loop (e.g. tests); swallow.
                pass
    except Exception:  # noqa: BLE001
        pass
