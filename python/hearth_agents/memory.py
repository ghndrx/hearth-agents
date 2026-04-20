"""Warm per-repo memory — persist what worked between feature runs.

Every verified-done feature appends a one-line JSONL entry per target repo
to ``/data/memory/<repo>.jsonl``. The orchestrator prompt pulls the top-3
most-recent entries for each target repo so the agent inherits tribal
knowledge instead of cold-starting on every feature.

This is a stopgap for the agents' own ``self-warm-memory`` backlog feature.
Kept tiny and file-based on purpose — we can swap in SQLite + embeddings
later without changing the consumer interface.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

MEM_DIR = Path("/data/memory")
RECENT_LIMIT = 3
MAX_LINE_LEN = 400  # clip long summaries so the prompt doesn't bloat


def _memfile(repo: str) -> Path:
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    return MEM_DIR / f"{repo}.jsonl"


def record_done(feature_id: str, feature_name: str, repos: list[str], summary: str) -> None:
    """Append one entry per target repo. Best-effort — never raises."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "feature_id": feature_id,
        "name": feature_name,
        "summary": summary[:MAX_LINE_LEN],
    }
    line = json.dumps(entry) + "\n"
    for repo in repos:
        try:
            with _memfile(repo).open("a") as f:
                f.write(line)
        except OSError:
            pass


def recent_for_repo(repo: str, limit: int = RECENT_LIMIT) -> list[dict]:
    path = _memfile(repo)
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def block_for_prompt(repos: list[str], limit: int = RECENT_LIMIT) -> str:
    """Format a compact markdown block ready to embed in the feature prompt."""
    sections: list[str] = []
    for repo in repos:
        entries = recent_for_repo(repo, limit=limit)
        if not entries:
            continue
        lines = [f"- **{e['feature_id']}**: {e.get('summary', '')}" for e in entries]
        sections.append(f"### Recent wins in {repo}\n" + "\n".join(lines))
    return "\n\n".join(sections)
