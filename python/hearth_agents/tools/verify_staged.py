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


_SYMBOL_UNRESOLVED_PATTERNS = (
    # Go: "undefined: gomatrixserverlib.FakeThing"
    "undefined:",
    "undefined reference",
    # Python: "NameError" / ruff F821 undefined-name / F401 unused-or-missing
    "NameError:",
    "F821",
    "F401",
    "ModuleNotFoundError:",
    "ImportError:",
    # TypeScript
    "Cannot find name",
    "Cannot find module",
    "TS2304",
    "TS2307",
    # Rust
    "cannot find function",
    "cannot find type",
    "unresolved import",
)


def _classify_failures(output: str) -> tuple[bool, list[str]]:
    """Inspect a failing build/lint/type-check output for hallucinated-API
    signatures. Returns (has_symbol_unresolved, matching_line_samples).

    The distinction matters: "undefined: FakeSDK.Method" is the agent
    inventing an API (research #3813 — hallucinated API detection) and
    needs different guidance than "TypeError on line 42" which is a real
    bug the agent can fix. Tagging here lets heal_hint route differently.
    """
    matches: list[str] = []
    if not output:
        return False, matches
    for line in output.splitlines():
        if any(sig in line for sig in _SYMBOL_UNRESOLVED_PATTERNS):
            matches.append(line.strip()[:160])
            if len(matches) >= 5:
                break
    return bool(matches), matches


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
    hallucinations: list[str] = []

    def _emit(section: str, c: int, o: str, tail: int = 800) -> None:
        status = "OK" if c == 0 else "FAIL"
        lines.append(f"\n### {section}\n{status}\n{o[-tail:] if o else ''}")
        if c != 0:
            found, samples = _classify_failures(o)
            if found:
                hallucinations.extend(f"{section}: {s}" for s in samples)

    if "go" in stacks:
        c, o = _run(["go", "build", "./..."], repo_path, timeout=120)
        _emit("go build ./...", c, o)
        c, o = _run(["go", "vet", "./..."], repo_path, timeout=60)
        _emit("go vet ./...", c, o, tail=400)
    if "py" in stacks:
        c, o = _run(["ruff", "check", *[p for p in staged if p.endswith('.py')]], repo_path, timeout=30)
        _emit("ruff check", c, o, tail=400)
    if "ts" in stacks and (Path(repo_path) / "package.json").exists():
        c, o = _run(["npx", "--no-install", "tsc", "--noEmit"], repo_path, timeout=180)
        _emit("tsc --noEmit", c, o)
    if "rs" in stacks:
        c, o = _run(["cargo", "check"], repo_path, timeout=180)
        _emit("cargo check", c, o)

    if hallucinations:
        lines.append(
            "\n### ⚠️ SYMBOL_UNRESOLVED (hallucinated APIs?)\n"
            "The following failures look like references to symbols that don't\n"
            "exist in the resolved dependencies. Before re-editing, confirm each\n"
            "symbol against the real package's docs — don't regenerate the same\n"
            "invented name. If the symbol really exists, check the import path:\n  "
            + "\n  ".join(hallucinations)
        )
    return "\n".join(lines)
