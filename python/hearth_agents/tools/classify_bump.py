"""Semver bump classifier for autonomous dependency updates.

Research #3828 (autonomous security vulnerability patching) found that
the winning pattern for LLM-driven dep bumps classifies the change
BEFORE invoking the LLM: patch bumps skip breaking-change analysis
entirely (token savings), minor/major bumps trigger migration reasoning.

Defensive about non-strict semver strings like ``2.0-beta9`` or
``v1.2.3``, which crash a naive ``int(x.split(".")[0])``.
"""

from __future__ import annotations

import re
from typing import Literal

from langchain_core.tools import tool

BumpKind = Literal["patch", "minor", "major", "unknown"]

_SEMVER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?([^\d].*)?$")


def _parse(version: str) -> tuple[int, int, int] | None:
    """Return (major, minor, patch) or None. Ignores pre-release suffix."""
    m = _SEMVER_RE.match(version.strip())
    if not m:
        return None
    try:
        return int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3) or 0)
    except ValueError:
        return None


def classify(cur_ver: str, tgt_ver: str) -> BumpKind:
    """Pure function classifier. ``unknown`` when either version can't
    be parsed — the caller should treat unknown like ``major`` (full
    audit) rather than like ``patch`` (skip audit)."""
    cur = _parse(cur_ver)
    tgt = _parse(tgt_ver)
    if cur is None or tgt is None:
        return "unknown"
    if cur[0] != tgt[0]:
        return "major"
    if cur[1] != tgt[1]:
        return "minor"
    if cur[2] != tgt[2]:
        return "patch"
    return "patch"  # identical versions; caller can short-circuit


@tool
def classify_bump(package: str, current_version: str, target_version: str) -> str:
    """Classify a dependency version bump as patch / minor / major /
    unknown. Use this BEFORE spending fixup budget on a dep-update
    feature: patch bumps can skip the full adversarial self-audit,
    major bumps need breaking-change migration analysis.

    Args:
        package: Name of the dep (for log context only, no fetch).
        current_version: Currently-pinned version string.
        target_version: Version you're bumping to.

    Returns:
        "patch" | "minor" | "major" | "unknown" plus a short explanation.
    """
    kind = classify(current_version, target_version)
    if kind == "unknown":
        return (
            f"unknown: could not parse {current_version!r} or {target_version!r} as semver. "
            "Treat as major (run full audit) to be safe."
        )
    return f"{kind}: {package} {current_version} -> {target_version}"
