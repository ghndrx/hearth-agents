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


def _remote_has_branch(repo_path: str, branch: str) -> bool:
    """Return True if ``branch`` exists on origin. Uses ``git ls-remote``.

    This catches the common failure mode where the agent commits locally but
    never pushes — the verifier's earlier "has commits" check passes while
    the work is actually invisible on GitHub.
    """
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "ls-remote", "--heads", "origin", branch],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except subprocess.TimeoutExpired:
        return False


def verify_changes(feature: Feature) -> tuple[bool, str]:
    """Check whether the feature actually produced *pushed* changes.

    Returns ``(ok, reason)``. ``ok`` is True if at least one target repo's
    ``feat/<feature_id>`` worktree has commits beyond its base branch AND
    the branch exists on the remote. Local-only commits no longer count —
    the whole point of this loop is visible PRs on GitHub.
    """
    branch = f"feat/{feature.id}"
    committed: list[str] = []
    pushed: list[str] = []

    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        wt = Path(repo_path).parent / f"worktrees-{Path(repo_path).name}" / branch
        if not wt.exists():
            continue
        base = "develop" if repo_name == "hearth" else "main"
        if _has_commits(wt, base):
            committed.append(repo_name)
            if _remote_has_branch(repo_path, branch):
                pushed.append(repo_name)

    if pushed:
        return True, f"pushed to: {', '.join(pushed)}"
    if committed:
        return False, f"committed locally on {', '.join(committed)} but never pushed {branch}"
    return False, f"no commits on any worktree for {branch}"
