"""External verifier for feature runs.

The agent's self-reported verdict cannot be trusted — it routinely says "done"
on features that produced zero file changes. This module provides a deterministic
check: did the feature's worktree actually diverge from its base branch?

Usage in ``run_once``: call ``verify_changes(feature)`` after ``agent.ainvoke``
and downgrade the verdict to ``blocked`` if no repo touched any files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .backlog import Feature
from .config import settings
from .logger import log


def _has_commits(worktree: Path, base: str) -> bool:
    """Return True if the worktree has at least one commit beyond ``base``."""
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "rev-list", "--count", f"{base}..HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return r.returncode == 0 and int((r.stdout or "0").strip()) > 0
    except (subprocess.TimeoutExpired, ValueError):
        return False


def verify_changes(feature: Feature) -> tuple[bool, str]:
    """Check whether the feature actually produced committed changes.

    Returns ``(ok, reason)``. ``ok`` is True if at least one target repo's
    ``feat/<feature_id>`` worktree has commits beyond its base branch.
    """
    branch = f"feat/{feature.id}"
    touched: list[str] = []

    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        wt = Path(repo_path).parent / f"worktrees-{Path(repo_path).name}" / branch
        if not wt.exists():
            continue
        base = "develop" if repo_name == "hearth" else "main"
        if _has_commits(wt, base):
            touched.append(repo_name)

    if touched:
        return True, f"commits on: {', '.join(touched)}"
    return False, f"no commits on any worktree for {branch}"
