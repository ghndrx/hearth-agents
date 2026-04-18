"""Pre-commit advisory verification.

``verify_staged(repo_path)`` runs the same lint/test checks ``verify_changes``
does, but BEFORE ``git_commit`` — inside the agent's own session, so the
agent can iterate on the failing output rather than losing a whole fixup
round to the post-commit verifier.

Distinct from the full verifier:
  - Operates on the staged diff, not the pushed HEAD
  - Does NOT enforce the 600-line cap, planner-undercount, or test-file
    requirement — those are correctness gates owned by ``verify_changes``
  - Returns a multi-line human-readable report, not a (bool, str) tuple
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import tool


def _run(cmd: list[str], cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"


def _detect_stacks(staged: list[str]) -> set[str]:
    """Return the set of language stacks the staged diff touches."""
    out: set[str] = set()
    for p in staged:
        if p.endswith(".go"):
            out.add("go")
        elif p.endswith((".ts", ".tsx", ".js", ".jsx", ".svelte")):
            out.add("ts")
        elif p.endswith(".py"):
            out.add("py")
        elif p.endswith(".rs"):
            out.add("rs")
    return out


@tool
def verify_staged(repo_path: str) -> str:
    """Run advisory build + lint + test checks against currently staged
    changes, BEFORE committing. Use this after ``write_file``/``edit_file``
    and ``git add``, just before ``git_commit``. Catches errors the
    post-commit verifier would catch, but in-session — so you can fix
    without losing a fixup round.

    Runs only the checks relevant to languages present in the staged
    diff. Does not modify files. Never fails: returns a report string
    even when checks fail; the agent reads the report and decides.

    Args:
        repo_path: Absolute path to the repo or worktree.
    """
    code, out = _run(["git", "diff", "--cached", "--name-only"], repo_path, timeout=10)
    if code != 0:
        return f"error listing staged files: {out}"
    staged = [p for p in out.splitlines() if p]
    if not staged:
        return "no staged changes; stage with git add before calling verify_staged"
    stacks = _detect_stacks(staged)
    if not stacks:
        return f"staged files use no recognized stack (files: {staged[:5]}); skipping"

    lines: list[str] = [f"## verify_staged: {len(staged)} file(s), stacks: {', '.join(sorted(stacks))}"]
    if "go" in stacks:
        c, o = _run(["go", "build", "./..."], repo_path, timeout=120)
        lines.append(f"\n### go build ./...\n{'OK' if c == 0 else 'FAIL'}\n{o[-800:] if o else ''}")
        c, o = _run(["go", "vet", "./..."], repo_path, timeout=60)
        lines.append(f"\n### go vet ./...\n{'OK' if c == 0 else 'FAIL'}\n{o[-400:] if o else ''}")
    if "py" in stacks:
        c, o = _run(["ruff", "check", *[p for p in staged if p.endswith('.py')]], repo_path, timeout=30)
        lines.append(f"\n### ruff check\n{'OK' if c == 0 else 'FAIL'}\n{o[-400:] if o else ''}")
    if "ts" in stacks and (Path(repo_path) / "package.json").exists():
        c, o = _run(["npx", "--no-install", "tsc", "--noEmit"], repo_path, timeout=180)
        lines.append(f"\n### tsc --noEmit\n{'OK' if c == 0 else 'FAIL'}\n{o[-800:] if o else ''}")
    if "rs" in stacks:
        c, o = _run(["cargo", "check"], repo_path, timeout=180)
        lines.append(f"\n### cargo check\n{'OK' if c == 0 else 'FAIL'}\n{o[-800:] if o else ''}")
    return "\n".join(lines)
