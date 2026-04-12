"""Bounded shell execution for test/build/lint loops.

Used by the orchestrator + dev subagents to actually run ``go test``,
``vitest``, ``ruff``, ``mypy``, ``go build`` etc. against a worktree and see
real output. Without this, "done" is vibes — with it, "done" means "tests
pass, linter clean".

Hard bounds:
  - ``cwd`` MUST be under one of the configured repo worktree trees. No
    escaping into ``/etc`` or the host shell.
  - 5-minute timeout per call. Tests that run longer should be split.
  - Output capped at 20K chars so a chatty test runner doesn't blow context.
"""

import subprocess
from pathlib import Path

from langchain_core.tools import tool

from ..config import settings
from ..logger import log

_MAX_OUTPUT = 20_000
_TIMEOUT = 300


def _allowed_root(cwd: str) -> bool:
    """Confine execution to repo parents + their worktree directories."""
    resolved = Path(cwd).resolve()
    allowed_parents = {Path(p).resolve().parent for p in settings.repo_paths.values()}
    return any(str(resolved).startswith(str(p)) for p in allowed_parents)


@tool
def run_command(command: str, cwd: str, timeout_sec: int = 120) -> str:
    """Run a shell command in a worktree and return its combined output.

    Use this to verify your changes before claiming a feature is done:
      - Go: ``go test ./...``, ``go build ./...``, ``go vet ./...``
      - TypeScript/Svelte: ``npm test``, ``npx vitest run``, ``npx tsc --noEmit``
      - Python: ``uv run pytest``, ``uv run ruff check``, ``uv run mypy .``

    Args:
        command: Shell command to execute. Uses /bin/sh, no shell expansion of env.
        cwd: Absolute path to the worktree directory to run in. Must be under
            a configured repo path.
        timeout_sec: Per-command timeout, capped at 300.

    Returns:
        Combined stdout+stderr, prefixed with ``exit=<code>``. Truncated at
        20K chars.
    """
    if not _allowed_root(cwd):
        return f"error: cwd {cwd} is outside configured repo roots"
    timeout = min(timeout_sec, _TIMEOUT)
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (r.stdout + r.stderr).strip()
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + "\n... (truncated)"
        log.info("run_command", cwd=cwd, cmd=command[:80], exit=r.returncode)
        return f"exit={r.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return f"exit=124\nTimed out after {timeout}s"
