"""Git operations for worktree-based feature branches.

Each feature gets its own worktree under ``worktrees-<repo>/<branch>/`` so
multiple features can be implemented concurrently without stepping on each other.
"""

import subprocess
from pathlib import Path

from langchain_core.tools import tool

from ..logger import log


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str]:
    """Run a subprocess, returning (exit_code, combined_output)."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"


@tool
def git_status(repo_path: str) -> str:
    """Return ``git status --short`` output for a repository.

    Args:
        repo_path: Absolute path to the git repo or worktree.
    """
    code, out = _run(["git", "status", "--short"], cwd=repo_path, timeout=10)
    return out or "(clean)" if code == 0 else f"error: {out}"


@tool
def git_commit(repo_path: str, message: str, add_all: bool = True) -> str:
    """Stage and commit changes in a repo.

    Args:
        repo_path: Absolute path to the repo/worktree.
        message: Commit message (use Conventional Commits: ``feat: ...``).
        add_all: Whether to ``git add -A`` first. Default True.
    """
    if add_all:
        code, out = _run(["git", "add", "-A"], cwd=repo_path, timeout=15)
        if code != 0:
            return f"git add failed: {out}"
    code, out = _run(["git", "commit", "-m", message], cwd=repo_path, timeout=15)
    return out if code == 0 else f"commit failed: {out}"


@tool
def git_branch_create(repo_path: str, branch: str, from_ref: str = "develop") -> str:
    """Create (or switch to) a feature branch.

    Args:
        repo_path: Absolute path to the repo.
        branch: New branch name, e.g. ``feat/matrix-federation``.
        from_ref: Base branch, default ``develop``.
    """
    code, out = _run(["git", "checkout", "-B", branch, from_ref], cwd=repo_path, timeout=15)
    return out if code == 0 else f"branch create failed: {out}"


@tool
def git_worktree_add(repo_path: str, branch: str, from_ref: str = "develop") -> str:
    """Create a git worktree for isolated implementation work.

    Places the worktree at ``<repo_parent>/worktrees-<repo_name>/<branch>/`` so
    the three target repos (hearth, hearth-desktop, hearth-mobile) never collide
    on the same path.

    Args:
        repo_path: Absolute path to the main repo.
        branch: Branch name.
        from_ref: Base branch, default ``develop``.

    Returns:
        Absolute path of the new worktree, or an error message.
    """
    repo = Path(repo_path).resolve()
    wt_dir = repo.parent / f"worktrees-{repo.name}" / branch
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    # Clean up any stale worktree or branch from prior runs so we get a fresh start.
    _run(["git", "worktree", "remove", str(wt_dir), "--force"], cwd=repo_path, timeout=10)
    _run(["git", "branch", "-D", branch], cwd=repo_path, timeout=10)
    _run(["git", "worktree", "prune"], cwd=repo_path, timeout=10)

    code, out = _run(
        ["git", "worktree", "add", "-B", branch, str(wt_dir), from_ref],
        cwd=repo_path,
        timeout=30,
    )
    if code != 0:
        log.warning("worktree_add_failed", branch=branch, error=out)
        return f"worktree add failed: {out}"
    log.info("worktree_created", path=str(wt_dir), branch=branch)
    return str(wt_dir)


@tool
def git_worktree_remove(worktree_path: str, delete_branch: bool = True) -> str:
    """Remove a worktree and optionally delete its branch. Call this when a
    feature is done or the implementation produced zero changes.

    Args:
        worktree_path: Absolute path from ``git_worktree_add``.
        delete_branch: Delete the feature branch too. Default True.
    """
    wt = Path(worktree_path).resolve()
    # Parent repo is two levels up: worktrees-<repo>/<branch>/ → worktrees-<repo> → repo parent
    repo_parent = wt.parent.parent.parent
    repo_name = wt.parent.name.removeprefix("worktrees-")
    repo_path = str(repo_parent / repo_name)
    branch = wt.name

    code, out = _run(["git", "worktree", "remove", str(wt), "--force"], cwd=repo_path, timeout=15)
    if delete_branch:
        _run(["git", "branch", "-D", branch], cwd=repo_path, timeout=10)
    _run(["git", "worktree", "prune"], cwd=repo_path, timeout=10)
    return out or f"removed {wt}" if code == 0 else f"remove failed: {out}"
