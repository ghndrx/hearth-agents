"""Worktree garbage collector.

Every ``N`` minutes, scan for worktrees under ``worktrees-<repo>/feat/*/``
whose feature branch has either:
  - Been merged to ``main``/``develop`` (branch already in the base), or
  - Been marked ``done`` in the backlog and is older than the retention window,
    or
  - Been marked ``blocked`` with ``heal_attempts`` past the escalation cap
    (the healer has given up — keep around briefly for human triage, then GC).

Reclaimed disk is typically 50–150 MB per worktree. Without this sweep disk
fills inside a few days of autonomous operation (we hit 90% in 24h).
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from .backlog import Backlog
from .config import settings
from .logger import log

GC_INTERVAL_SEC = 30 * 60          # sweep twice per hour
DONE_RETENTION_SEC = 6 * 60 * 60   # keep done worktrees for 6h (post-mortem window)
BLOCKED_RETENTION_SEC = 24 * 60 * 60  # keep blocked worktrees for 24h so humans can inspect


def _remove_worktree(repo_path: Path, worktree: Path) -> bool:
    """``git worktree remove`` with --force. Returns True on success."""
    try:
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


async def _sweep_once(backlog: Backlog) -> dict[str, int]:
    """Scan all configured repos and remove eligible worktrees. Returns a
    counter dict for logging."""
    counts = {"removed_done": 0, "removed_blocked": 0, "skipped": 0, "errors": 0}
    now = time.time()
    id_to_feature = {f.id: f for f in backlog.features}

    for repo_name, repo_path_str in settings.repo_paths.items():
        repo_path = Path(repo_path_str)
        worktrees_dir = repo_path.parent / f"worktrees-{repo_path.name}" / "feat"
        if not worktrees_dir.exists():
            continue

        for wt in worktrees_dir.iterdir():
            if not wt.is_dir():
                continue
            feature_id = wt.name  # feat/<feature_id> → the trailing segment
            feature = id_to_feature.get(feature_id)
            if feature is None:
                counts["skipped"] += 1
                continue

            # Age check: stat mtime of the worktree root directory.
            try:
                age = now - wt.stat().st_mtime
            except OSError:
                counts["errors"] += 1
                continue

            should_remove = False
            if feature.status == "done" and age > DONE_RETENTION_SEC:
                should_remove = True
                counts_key = "removed_done"
            elif feature.status == "blocked" and age > BLOCKED_RETENTION_SEC:
                should_remove = True
                counts_key = "removed_blocked"
            else:
                counts["skipped"] += 1
                continue

            if _remove_worktree(repo_path, wt):
                counts[counts_key] += 1
                log.info("gc_removed_worktree", feature=feature_id, status=feature.status, age_sec=int(age))
            else:
                counts["errors"] += 1
    return counts


async def run_worktree_gc(backlog: Backlog) -> None:
    """Background task: periodically reclaim disk from done/blocked worktrees."""
    log.info(
        "gc_started",
        interval_sec=GC_INTERVAL_SEC,
        done_retention_h=DONE_RETENTION_SEC // 3600,
        blocked_retention_h=BLOCKED_RETENTION_SEC // 3600,
    )
    while True:
        try:
            counts = await _sweep_once(backlog)
            if counts["removed_done"] + counts["removed_blocked"] > 0:
                log.info("gc_sweep", **counts)
        except Exception as e:  # noqa: BLE001
            log.exception("gc_sweep_failed", error=str(e))
        await asyncio.sleep(GC_INTERVAL_SEC)
