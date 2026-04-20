"""Persist wikidelve research jobs so future agent runs can reference them.

Every call to ``wikidelve_research`` appends to ``/data/research_jobs.jsonl``:
``{ts, job_id, topic, status}``. Status starts as ``queued``; a polling task
flips it to ``complete`` when wikidelve_search finds an article matching the
topic slug (or when status endpoint reports done, if available).

This lets agents call ``wikidelve_pending_jobs()`` to see what's still cooking
from their prior invocations instead of re-queueing the same research.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JOBS_PATH = Path("/data/research_jobs.jsonl")


def record_job(job_id: str, topic: str) -> None:
    """Append a newly-queued research job. Best-effort — never raises."""
    try:
        JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "job_id": job_id,
            "topic": topic,
            "status": "queued",
        }
        with JOBS_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _load_all() -> list[dict[str, Any]]:
    if not JOBS_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    with JOBS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def list_pending(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most-recent queued jobs, oldest first."""
    jobs = [j for j in _load_all() if j.get("status") == "queued"]
    return jobs[-limit:]


def list_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most-recent jobs of any status."""
    return _load_all()[-limit:]


def mark_complete(job_id: str) -> None:
    """Rewrite the log, flipping the matching job's status to ``complete``."""
    jobs = _load_all()
    changed = False
    for j in jobs:
        if j.get("job_id") == job_id and j.get("status") != "complete":
            j["status"] = "complete"
            j["completed_ts"] = datetime.now(UTC).isoformat()
            changed = True
    if not changed:
        return
    tmp = JOBS_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for j in jobs:
            f.write(json.dumps(j) + "\n")
    tmp.replace(JOBS_PATH)
