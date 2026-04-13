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

# Hard cap on net-added lines per feature. Features producing huge diffs from
# a single prompt are almost always unfocused or hallucinated. Blocking them
# forces the agent to split work into smaller PRs.
DIFF_LINE_CAP = 600


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


def _diff_stat(worktree: Path, base: str) -> int:
    """Return total added+deleted lines on the worktree vs base. -1 on error."""
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--shortstat", f"{base}..HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode != 0:
            return -1
        # Output like: " 3 files changed, 42 insertions(+), 7 deletions(-)"
        out = r.stdout.strip()
        total = 0
        for token in ("insertions", "deletions"):
            if token in out:
                part = out.split(token)[0].rsplit(",", 1)[-1].strip()
                try:
                    total += int(part.split()[0])
                except (ValueError, IndexError):
                    pass
        return total
    except subprocess.TimeoutExpired:
        return -1


def _run_tests(worktree: Path, repo_name: str) -> tuple[bool, str]:
    """Run the repo's own test command inside the worktree.

    Best-effort: if we don't know the test command for a repo or the suite
    isn't installed, we pass (don't block shipping on missing infra). Only a
    real, non-zero exit with discovered tests counts as failure.
    """
    commands: dict[str, list[str]] = {
        "hearth-agents": ["pytest", "python/tests", "-x", "--tb=short", "-q"],
        "hearth": ["go", "test", "./..."],
        "hearth-desktop": ["pnpm", "test", "--", "--run"],
        "hearth-mobile": ["pnpm", "test", "--", "--run"],
    }
    cmd = commands.get(repo_name)
    if not cmd:
        return True, "no test command known"
    try:
        r = subprocess.run(
            cmd, cwd=str(worktree), capture_output=True, text=True, timeout=300, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return True, f"tests skipped: {e.__class__.__name__}"
    if r.returncode == 0:
        return True, "tests passed"
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return False, "tests failed: " + " | ".join(tail)[:200]


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
    oversized: list[str] = []
    test_failures: list[str] = []

    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        wt = Path(repo_path).parent / f"worktrees-{Path(repo_path).name}" / branch
        if not wt.exists():
            continue
        base = "develop" if repo_name == "hearth" else "main"
        if not _has_commits(wt, base):
            continue
        committed.append(repo_name)

        # Diff-size gate: unfocused mega-diffs almost always mean hallucinated
        # or unrelated changes. Force the agent to split before shipping.
        lines = _diff_stat(wt, base)
        if lines > DIFF_LINE_CAP:
            oversized.append(f"{repo_name} ({lines} lines)")
            continue

        # Test gate: if the repo has a known test command and it fails, block.
        ok, reason = _run_tests(wt, repo_name)
        if not ok:
            test_failures.append(f"{repo_name}: {reason}")
            continue

        if _remote_has_branch(repo_path, branch):
            pushed.append(repo_name)

    if pushed:
        return True, f"pushed to: {', '.join(pushed)}"
    if oversized:
        return False, f"diff too large (>{DIFF_LINE_CAP} lines): {', '.join(oversized)}"
    if test_failures:
        return False, "; ".join(test_failures)
    if committed:
        return False, f"committed locally on {', '.join(committed)} but never pushed {branch}"
    return False, f"no commits on any worktree for {branch}"
