"""Append-only transition log for feature status changes.

Each status change writes one JSON line to ``/data/transitions.jsonl`` so
operators can answer "why did feature X move to blocked at 14:07?" by
grepping this file or tailing it live. Also the seed for future
event-sourcing of the kanban board (research #3802).

Lines are never mutated or deleted in place — this file is the audit
trail, not a cache. If disk becomes a concern, log-rotate externally.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .logger import log

_DEFAULT_PATH = Path(os.environ.get("TRANSITIONS_PATH", "/data/transitions.jsonl"))


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
    }
    try:
        _DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEFAULT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("transition_log_write_failed", err=str(e)[:200], feature=feature_id)
