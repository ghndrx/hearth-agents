"""Git operations for worktree-based feature branches.

Each feature gets its own worktree under ``worktrees-<repo>/<branch>/`` so
multiple features can be implemented concurrently without stepping on each other.
"""

import subprocess
from pathlib import Path

from langchain_core.tools import tool

from ..config import settings
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


_DIFF_SOFT_WARN = 200
_DIFF_HARD_WARN = 400


@tool
def git_status(repo_path: str) -> str:
    """Return ``git status --short`` output for a repository, plus a
    diff-size summary against the base branch when the worktree is on a
    feature branch. The summary surfaces mid-stream so the agent can split
    or wrap up BEFORE hitting the 600-line verifier cap (which has stranded
    multiple features at >2000 lines).

    Args:
        repo_path: Absolute path to the git repo or worktree.
    """
    code, out = _run(["git", "status", "--short"], cwd=repo_path, timeout=10)
    if code != 0:
        return f"error: {out}"
    status = out or "(clean)"

    # Determine current branch + base. develop for hearth, main everywhere else.
    bcode, branch = _run(
        ["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_path, timeout=5
    )
    if bcode != 0 or not branch.strip().startswith("feat/"):
        return status
    # Base-branch detection: hearth uses 'develop', other repos use 'main'.
    # Worktree paths are /repos/worktrees-hearth/feat/<name> — the old
    # "/hearth/" substring check missed them (they contain "worktrees-
    # hearth", not "/hearth/"). Match both primary (/repos/hearth...) and
    # worktree (/repos/worktrees-hearth/...) layouts. Excludes hearth-
    # desktop, hearth-mobile, hearth-agents whose repo names merely start
    # with 'hearth' but use 'main'.
    is_hearth_repo = (
        "/repos/hearth/" in f"{repo_path}/"
        or "/repos/worktrees-hearth/" in f"{repo_path}/"
    )
    base = "develop" if is_hearth_repo else "main"
    dcode, dout = _run(
        ["git", "diff", "--shortstat", f"{base}...HEAD"], cwd=repo_path, timeout=10
    )
    if dcode != 0 or not dout.strip():
        # On a feature branch with ZERO diff vs base — you've created the
        # branch but haven't written anything yet. This is the canonical
        # "no commits on any worktree" failure mode caught early. Nudge the
        # agent explicitly rather than silently returning a bare status.
        return (
            f"{status}\n\n"
            f"📝 branch {branch.strip()} has NO diff vs {base} yet. "
            "If you've already read the relevant files, your next action "
            "should be write_file or edit_file (not another read). If the "
            "feature is genuinely blocked, return the message "
            "'BLOCKED: <concrete reason>' instead of another exploratory read."
        )
    # shortstat format: " 3 files changed, 47 insertions(+), 12 deletions(-)"
    import re as _re
    ins = sum(int(m) for m in _re.findall(r"(\d+) insertion", dout))
    dele = sum(int(m) for m in _re.findall(r"(\d+) deletion", dout))
    total = ins + dele
    note = ""
    if total >= _DIFF_HARD_WARN:
        note = (
            f"\n⚠️  DIFF SIZE WARNING: {total} lines vs {base} (cap is 600). "
            "WRAP UP NOW — make any final edits, run tests, commit. Do not "
            "add more files. If the feature genuinely needs more, stop and "
            "report 'BLOCKED: needs decomposition'."
        )
    elif total >= _DIFF_SOFT_WARN:
        note = (
            f"\n📏 diff size: {total} lines vs {base} (soft warn at "
            f"{_DIFF_SOFT_WARN}, hard at {_DIFF_HARD_WARN}, cap at 600). "
            "Stay focused on the feature scope; defer secondary concerns."
        )
    return f"{status}\n\ndiff vs {base}: {dout.strip()}{note}"


# Dependency-cache / build-artifact DIRECTORIES (substring match).
_BLOCKED_COMMIT_PATTERNS = (
    "node_modules/",
    ".pnpm-store/",
    "dist/",
    "build/",
    "target/",
    ".next/",
    ".svelte-kit/",
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".turbo/",
    "coverage/",
)

# Lock FILES are trickier: sometimes legitimately updated, but when the agent
# just ran `pnpm install` they produce 50k-line noise that dominates diffs
# and trips the size cap. Observed in prod: features with 849k-line diffs
# where ~95% was lock-file churn. Scrubbing is the right default; if a
# feature truly needs a dep bump, a planner note should explicitly say so.
_BLOCKED_LOCK_FILES = (
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "Gemfile.lock",
    "uv.lock",
)

# One-off hack files previous runs of the agent created (e.g. dummy-push-
# trigger.txt) that should never land in production commits.
_BLOCKED_DEBRIS_FILES = (
    "dummy-push-trigger.txt",
    "dummy-trigger.txt",
    "push-trigger.txt",
)


def _auto_format(repo_path: str) -> list[str]:
    """Run language-appropriate formatters on staged files before commit.

    Formatting-only churn (spacing, import ordering, trailing commas)
    routinely pushed agent diffs past the 600-line cap even when the
    semantic change was tiny. Running the formatter the human project
    already uses strips this noise at the source.

    Best-effort: every formatter is optional and errors are logged but
    swallowed. Returns the list of formatters that actually ran.
    """
    code, out = _run(["git", "diff", "--cached", "--name-only"], cwd=repo_path, timeout=10)
    if code != 0 or not out:
        return []
    paths = [p for p in out.splitlines() if p]
    if not paths:
        return []
    go_files = [p for p in paths if p.endswith(".go")]
    py_files = [p for p in paths if p.endswith(".py")]
    ts_files = [p for p in paths if p.endswith((".ts", ".tsx", ".js", ".jsx", ".svelte", ".json", ".yml", ".yaml", ".md"))]
    rs_files = [p for p in paths if p.endswith(".rs")]
    ran: list[str] = []
    # gofmt -w is idempotent and ships with every Go toolchain
    if go_files:
        r = _run(["gofmt", "-w", *go_files], cwd=repo_path, timeout=30)
        if r[0] == 0:
            ran.append("gofmt")
    # ruff is already a dev dep; format before check so format can clean
    # whitespace without lint triggering on the same lines
    if py_files:
        r = _run(["ruff", "format", *py_files], cwd=repo_path, timeout=30)
        if r[0] == 0:
            ran.append("ruff format")
    # prettier only if a local binary or package.json config is present;
    # `npx --no-install` keeps us from pulling the world when unavailable
    if ts_files:
        r = _run(["npx", "--no-install", "prettier", "--write", *ts_files], cwd=repo_path, timeout=45)
        if r[0] == 0:
            ran.append("prettier")
    if rs_files:
        r = _run(["rustfmt", *rs_files], cwd=repo_path, timeout=30)
        if r[0] == 0:
            ran.append("rustfmt")
    if ran:
        # Re-stage any files the formatter touched so the commit captures
        # the formatted version, not the pre-format staging.
        _run(["git", "add", *paths], cwd=repo_path, timeout=10)
    return ran


def _scrub_blocked_paths(repo_path: str) -> tuple[int, list[str]]:
    """Unstage any paths matching known build-artifact / dependency-cache
    patterns before commit. Returns (unstaged_count, sample_paths).

    Observed in production: one feature committed the repo's ``.pnpm-store``
    producing a 444,076-line diff that tripped the verifier's size cap.
    Rather than rejecting the commit entirely, we scrub the offending paths
    and let the real code-change commit through. Keeps the agent unblocked
    while keeping the diff clean.
    """
    code, out = _run(["git", "diff", "--cached", "--name-only"], cwd=repo_path, timeout=10)
    if code != 0:
        return 0, []
    paths = out.splitlines()
    bad = [
        p for p in paths
        if any(sig in p for sig in _BLOCKED_COMMIT_PATTERNS)
        or any(p.endswith(lock) for lock in _BLOCKED_LOCK_FILES)
        or any(p.endswith(debris) for debris in _BLOCKED_DEBRIS_FILES)
    ]
    if not bad:
        return 0, []
    # git rm --cached -r -- <path> preserves the worktree file but unstages it
    for path in bad:
        _run(["git", "rm", "--cached", "-r", "--", path], cwd=repo_path, timeout=10)
    return len(bad), bad[:5]


@tool
def git_commit(repo_path: str, message: str, add_all: bool = True, push: bool = True) -> str:
    """Stage, commit, and (by default) push the current branch.

    Commit and push are coupled because earlier runs committed locally and
    never pushed, leaving origin out of sync and the verifier retrying forever.
    Pushing here makes "committed" mean "durable on origin" for the pipeline.

    Before committing, scrubs known build-artifact paths (node_modules/,
    .pnpm-store/, dist/, etc.) from the staging area. Addresses the
    400k-line-diff failure mode caused by one feature committing .pnpm-store.

    Args:
        repo_path: Absolute path to the repo/worktree.
        message: Commit message (use Conventional Commits: ``feat: ...``).
        add_all: Whether to ``git add -A`` first. Default True.
        push: Whether to ``git push -u origin HEAD`` after commit. Default True.
            Pass False only for experimental / throwaway commits.
    """
    if add_all:
        code, out = _run(["git", "add", "-A"], cwd=repo_path, timeout=15)
        if code != 0:
            return f"git add failed: {out}"
    # Scrub blocked paths AFTER staging so `git add -A` can't resurrect them.
    scrubbed, sample = _scrub_blocked_paths(repo_path)
    if scrubbed:
        log.warning("git_commit_scrubbed_paths", repo=repo_path, count=scrubbed, sample=sample)
    # Auto-format staged files so spacing/import-order churn doesn't push
    # the diff past the 600-line cap. Runs per-language; each formatter is
    # optional. Re-stages in-place when something runs.
    formatters = _auto_format(repo_path)
    if formatters:
        log.info("git_commit_auto_formatted", repo=repo_path, formatters=formatters)
    code, out = _run(["git", "commit", "-m", message], cwd=repo_path, timeout=15)
    if code != 0:
        return f"commit failed: {out}"
    commit_out = out
    if not push:
        return commit_out
    pcode, pout = _run(["git", "push", "-u", "origin", "HEAD"], cwd=repo_path, timeout=60)
    if pcode != 0:
        # Push failure is a real failure — the iterate loop should retry rather
        # than proceed as if the commit is durable. Surface the error clearly.
        log.warning("git_push_failed", repo=repo_path, error=pout)
        return f"commit ok but push failed: {pout}"
    return f"{commit_out}\npushed: {pout}"


@tool
def git_branch_create(repo_path: str, branch: str, from_ref: str = "develop") -> str:
    """Create (or switch to) a feature branch in a WORKTREE path, never a primary repo.

    Refuses to ``git checkout -B`` inside a primary repo — doing so moves the
    primary off its base branch and leaks uncommitted feature cruft into
    every subsequent worker that cds into it. Use ``git_worktree_add`` for
    feature work; this tool is only useful inside an existing worktree.

    Args:
        repo_path: Absolute path to a worktree (NOT a primary repo).
        branch: New branch name, e.g. ``feat/matrix-federation``.
        from_ref: Base branch, default ``develop``.
    """
    from ..config import settings
    primaries = {str(Path(p).resolve()) for p in settings.repo_paths.values()}
    if str(Path(repo_path).resolve()) in primaries:
        return (
            "error: git_branch_create refuses to mutate a primary repo. "
            "Use ``git_worktree_add(repo_path, branch, from_ref)`` to get "
            "an isolated worktree for feature work. Primary repos stay on "
            "their base branch so other workers see a clean tree."
        )
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


@tool
def git_push(repo_path: str, branch: str, set_upstream: bool = True) -> str:
    """Push a branch to origin.

    Uses GITHUB_TOKEN from environment/config for authentication.
    Automatically injects the token into the remote URL if present.

    Args:
        repo_path: Absolute path to the repo/worktree.
        branch: Branch name to push.
        set_upstream: Whether to set upstream tracking (-u flag). Default True.
    """
    token = settings.github_token
    original_url = ""
    if token:
        # Temporarily inject token into remote URL for this push
        code, original_url = _run(["git", "remote", "get-url", "origin"], cwd=repo_path, timeout=10)
        # Convert https://github.com/user/repo.git -> https://x-access-token:TOKEN@github.com/user/repo.git
        # Note: GitHub requires x-access-token:TOKEN format (oauth2:TOKEN is deprecated)
        if code == 0 and original_url and original_url.startswith("https://"):
            # Strip any existing auth from URL
            clean_url = original_url
            if "@" in clean_url.replace("://", "").split("/")[0]:
                clean_url = "https://" + clean_url.split("@", 1)[1]
            new_url = f"https://x-access-token:{token}@{clean_url.replace('https://', '')}"
            _run(["git", "remote", "set-url", "origin", new_url], cwd=repo_path, timeout=10)

    cmd = ["git", "push"]
    if set_upstream:
        cmd.append("-u")
    cmd.extend(["origin", branch])

    code, out = _run(cmd, cwd=repo_path, timeout=30)

    if token and original_url:
        # Restore original URL without token for security
        _run(["git", "remote", "set-url", "origin", original_url], cwd=repo_path, timeout=10)

    return out if code == 0 else f"push failed: {out}"


def open_pr_if_possible(repo_path: str, branch: str, title: str, body: str) -> str:
    """Open a pull request on origin for ``branch`` → default-base.

    Plain function (not a @tool) so the loop can call it unconditionally
    when a feature lands, without relying on the orchestrator to choose.
    Agents can still open PRs by running ``gh`` via run_command if needed.

    Returns a short status string; never raises. Requires
    ``settings.github_token`` with ``repo`` scope. Skips silently when the
    token is absent, when the PR already exists, or when the remote isn't
    a GitHub HTTPS URL (self-hosted / git-ssh repos bypass this path).
    """
    token = settings.github_token
    if not token:
        return "skipped: no GITHUB_TOKEN"
    code, origin_url = _run(["git", "remote", "get-url", "origin"], cwd=repo_path, timeout=10)
    if code != 0 or "github.com" not in origin_url:
        return f"skipped: remote not on github.com ({origin_url[:80]})"
    # Parse owner/repo from https://github.com/OWNER/REPO(.git) — strip any
    # embedded token first in case git_push left one in the URL.
    clean = origin_url
    if "@" in clean.replace("://", "").split("/")[0]:
        clean = "https://" + clean.split("@", 1)[1]
    parts = clean.rstrip("/").replace(".git", "").split("/")
    if len(parts) < 5:
        return f"skipped: cannot parse repo from {clean[:80]}"
    owner, repo = parts[-2], parts[-1]
    # Default base branch: develop for hearth (see git_ops.py:59), main elsewhere.
    base = "develop" if repo == "hearth" else "main"
    # httpx is already a project dep; keep this import local so the test
    # suite's lazy-import stays lazy.
    import httpx
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title[:200], "head": branch, "base": base, "body": body[:5000]},
            )
    except httpx.HTTPError as e:
        return f"pr_api_error: {e}"
    if r.status_code == 201:
        url = r.json().get("html_url", "(no url)")
        log.info("pr_opened", repo=repo, branch=branch, url=url)
        return f"opened {url}"
    if r.status_code == 422:
        # 422 typically means "A pull request already exists for head/branch".
        # Treat as success — the earlier PR is what we wanted.
        log.info("pr_already_exists", repo=repo, branch=branch)
        return "pr already exists"
    log.warning("pr_open_failed", repo=repo, branch=branch, status=r.status_code, body=r.text[:200])
    return f"pr_open_failed: {r.status_code} {r.text[:120]}"
