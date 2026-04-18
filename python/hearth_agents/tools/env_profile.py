"""Environment snapshot + drift detection.

Research #3826 (autonomous code migration): ~30% of runtime errors in
cross-version migrations slip past static analysis because the agent
reasons about code without reasoning about the installed environment.
This tool snapshots version-relevant manifests (``go.mod``,
``package.json``, ``pyproject.toml``, ``Cargo.toml``) at feature start
and on each verify pass, surfacing drift as ENV_DRIFT alongside the
existing SYMBOL_UNRESOLVED tag.

Cheap and additive — no LSP, no container profiling. Just file reads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from langchain_core.tools import tool

_MANIFESTS = ("go.mod", "package.json", "pyproject.toml", "Cargo.toml", "uv.lock")


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def snapshot(repo_path: str) -> dict[str, str]:
    """Return a dict of {manifest_name: content_hash} for the repo root
    and first-level subdirectories. Missing manifests are omitted."""
    root = Path(repo_path)
    out: dict[str, str] = {}
    for name in _MANIFESTS:
        p = root / name
        if p.exists():
            h = _hash(p)
            if h:
                out[name] = h
    # First-level subdirs (e.g. python/pyproject.toml in hearth-agents)
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            for name in _MANIFESTS:
                p = child / name
                if p.exists():
                    h = _hash(p)
                    if h:
                        out[f"{child.name}/{name}"] = h
    return out


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return human-readable drift lines. Empty list when unchanged."""
    lines: list[str] = []
    all_keys = set(before) | set(after)
    for k in sorted(all_keys):
        b = before.get(k, "(absent)")
        a = after.get(k, "(removed)")
        if b != a:
            lines.append(f"  {k}: {b} -> {a}")
    return lines


@tool
def env_profile(repo_path: str) -> str:
    """Snapshot the current state of dependency manifests (go.mod,
    package.json, pyproject.toml, Cargo.toml, uv.lock) in the worktree.

    Use this at feature start when migrating frameworks or upgrading
    language versions: call it once before your edits, save the output,
    call it again after edits and compare. Manifest-level drift that
    you didn't plan for is a red flag — you probably pulled in an
    unintended transitive bump.

    Args:
        repo_path: Absolute worktree path.

    Returns:
        Newline-separated ``filename: hash`` entries, or ``(no manifests)``.
    """
    snap = snapshot(repo_path)
    if not snap:
        return "(no manifests found at this path)"
    return "\n".join(f"{k}: {v}" for k, v in sorted(snap.items()))
