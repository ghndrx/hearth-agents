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
# Block any single function exceeding this cyclomatic complexity. Research on
# AI-generated code quality names cyclomatic complexity as the strongest
# differentiator; 10 is the classic McCabe threshold.
MAX_FUNCTION_COMPLEXITY = 10


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


def _complexity_check(worktree: Path, repo_name: str) -> tuple[bool, str]:
    """Reject features whose diff adds any function with cyclomatic complexity > threshold.

    Python: ``radon cc -s -n E`` lists only grade E (very complex). We treat
    any E-or-worse function added by the diff as a block. Go: ``gocyclo -over N``
    if the binary is on PATH. Other languages default to pass.
    """
    if repo_name == "hearth-agents":
        # Only flag functions in files the feature actually touched, not the
        # whole codebase. `git diff --name-only base..HEAD -- *.py` narrows scope.
        try:
            files = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--name-only", "--diff-filter=AM",
                 f"main..HEAD", "--", "*.py"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.splitlines()
        except subprocess.TimeoutExpired:
            return True, "complexity skipped (git diff timeout)"
        if not files:
            return True, "complexity ok (no py files touched)"
        try:
            r = subprocess.run(
                ["radon", "cc", "-s", "-n", "D", *files],
                cwd=str(worktree), capture_output=True, text=True, timeout=30, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "complexity skipped (radon unavailable)"
        # radon prints nothing when all functions are below the threshold.
        flagged = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        if flagged:
            return False, f"complexity too high (>{MAX_FUNCTION_COMPLEXITY}): {flagged[0][:160]}"
        return True, "complexity ok"

    if repo_name == "hearth":
        try:
            r = subprocess.run(
                ["gocyclo", "-over", str(MAX_FUNCTION_COMPLEXITY), "backend"],
                cwd=str(worktree), capture_output=True, text=True, timeout=30, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "complexity skipped (gocyclo unavailable)"
        if r.stdout.strip():
            first = r.stdout.strip().splitlines()[0]
            return False, f"complexity too high (>{MAX_FUNCTION_COMPLEXITY}): {first[:160]}"
        return True, "complexity ok"

    return True, "complexity not checked for this repo"


def _run_tests(worktree: Path, repo_name: str) -> tuple[bool, str]:
    """Run the repo's own test command inside the worktree.

    Best-effort: if we don't know the test command for a repo or the suite
    isn't installed, we pass (don't block shipping on missing infra). Only a
    real, non-zero exit with discovered tests counts as failure.
    """
    # Per-repo: (cwd relative to worktree, env overrides, command)
    commands: dict[str, tuple[str, dict[str, str], list[str]]] = {
        "hearth-agents": ("python", {"PYTHONPATH": "."}, ["pytest", "tests", "-x", "--tb=short", "-q"]),
        "hearth": ("backend", {}, ["go", "test", "./..."]),
        "hearth-desktop": (".", {}, ["pnpm", "test", "--", "--run"]),
        "hearth-mobile": (".", {}, ["pnpm", "test", "--", "--run"]),
    }
    spec = commands.get(repo_name)
    if not spec:
        return True, "no test command known"
    subdir, env_overrides, cmd = spec
    import os as _os
    env = {**_os.environ, **env_overrides}
    try:
        r = subprocess.run(
            cmd, cwd=str(worktree / subdir), env=env,
            capture_output=True, text=True, timeout=300, check=False,
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


def _diff_is_prompt_only(worktree: Path, base: str, repo_name: str) -> bool:
    """True iff every changed file in this worktree is a prompt-only edit.

    Currently only ``hearth-agents/python/hearth_agents/prompts.py`` qualifies.
    For other repos we always return False so the test gate stays mandatory.
    """
    if repo_name != "hearth-agents":
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if r.returncode != 0:
        return False
    files = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    if not files:
        return False
    return all(f == "python/hearth_agents/prompts.py" for f in files)


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
    undercount: list[str] = []
    test_failures: list[str] = []
    complexity_failures: list[str] = []

    # Undercount threshold from research job #3673: 1.5x the planner's estimate
    # is the sweet spot — tight enough to catch runaway implementations, loose
    # enough to tolerate normal variance. Only enforced when the planner
    # actually recorded an estimate (``record_planner_estimate`` tool).
    UNDERCOUNT_RATIO = 1.5

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

        # Planner-undercount gate: if the planner recorded an estimate and the
        # actual diff overshot it by >UNDERCOUNT_RATIO, block this run. The
        # heal_hint path will surface "planner_undercount" to the next attempt
        # so the orchestrator can re-plan with a larger estimate or split
        # instead of silently chewing to the hard diff cap.
        est = getattr(feature, "planner_estimate_lines", 0)
        if est > 0 and lines > est * UNDERCOUNT_RATIO:
            undercount.append(f"{repo_name} ({lines} actual vs {est} estimated, {lines/est:.1f}x)")
            continue

        # Complexity gate: any function with cyclomatic complexity over the
        # threshold signals spaghetti the agent generated without refactoring.
        ok, reason = _complexity_check(wt, repo_name)
        if not ok:
            complexity_failures.append(f"{repo_name}: {reason}")
            continue

        # Test gate: if the repo has a known test command and it fails, block.
        # Exception: prompt-only diffs in hearth-agents have no testable behavior
        # in pytest — the change is just string content read at runtime. Forcing
        # a new test for every prompt tweak just blocks legitimate work, which
        # is exactly why earlier self-tune features kept landing in `blocked`.
        if not _diff_is_prompt_only(wt, base, repo_name):
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
    if undercount:
        return False, f"planner_undercount: {', '.join(undercount)}"
    if complexity_failures:
        return False, "; ".join(complexity_failures)
    if test_failures:
        return False, "; ".join(test_failures)
    if committed:
        return False, f"committed locally on {', '.join(committed)} but never pushed {branch}"
    return False, f"no commits on any worktree for {branch}"
