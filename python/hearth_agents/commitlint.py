"""Conventional Commits parser + bump classifier.

Research #3834 (autonomous release engineering): conventional commit
type determines semver bump — feat→minor, fix→patch, anything with
``!`` or ``BREAKING CHANGE:`` footer → major. Used by:
  - Auto-PR body to group commits into a changelog section
  - Future release bot to decide the next version tag
  - Webhook gate (we can flag incoming PR titles that aren't conventional)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

BumpKind = Literal["patch", "minor", "major", "none"]

_HEADER_RE = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<summary>.+)$")
_FOOTER_BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

_TYPE_TO_BUMP: dict[str, BumpKind] = {
    "feat": "minor",
    "fix": "patch",
    "perf": "patch",
    "refactor": "patch",
    "revert": "patch",
    "chore": "none",
    "docs": "none",
    "test": "none",
    "build": "none",
    "ci": "none",
    "style": "none",
    "deploy": "none",
    "gitops": "none",
    "security": "patch",
}

_CHANGELOG_SECTION: dict[str, str] = {
    "feat": "### Features",
    "fix": "### Bug Fixes",
    "perf": "### Performance",
    "security": "### Security",
    "refactor": "### Refactoring",
    "revert": "### Reverts",
}


@dataclass
class ParsedCommit:
    type: str
    scope: str
    breaking: bool
    summary: str
    body: str
    bump: BumpKind
    raw: str


def parse(message: str) -> ParsedCommit | None:
    """Parse a commit message. Returns None when the header doesn't match
    the conventional format at all."""
    if not message:
        return None
    header, _, rest = message.partition("\n")
    m = _HEADER_RE.match(header.strip())
    if not m:
        return None
    ctype = m.group("type").lower()
    scope = m.group("scope") or ""
    bang = bool(m.group("bang"))
    summary = m.group("summary").strip()
    body = rest.strip()
    breaking = bang or bool(_FOOTER_BREAKING_RE.search(message))
    if breaking:
        bump: BumpKind = "major"
    else:
        bump = _TYPE_TO_BUMP.get(ctype, "none")
    return ParsedCommit(
        type=ctype, scope=scope, breaking=breaking,
        summary=summary, body=body, bump=bump, raw=message,
    )


def next_bump(commits: list[ParsedCommit]) -> BumpKind:
    """Highest bump across a list of commits. ``none`` if nothing
    material landed (all chore/docs/ci)."""
    order = ["none", "patch", "minor", "major"]
    best = 0
    for c in commits:
        best = max(best, order.index(c.bump))
    return order[best]  # type: ignore[return-value]


def render_changelog(commits: list[ParsedCommit]) -> str:
    """Keep-a-Changelog-style grouping. Excludes ``none``-bump sections
    from the user-facing output; build/chore/ci noise belongs in the
    commit log, not the release notes."""
    from collections import defaultdict
    grouped: dict[str, list[str]] = defaultdict(list)
    for c in commits:
        section = _CHANGELOG_SECTION.get(c.type)
        if not section:
            continue
        scope_prefix = f"**{c.scope}**: " if c.scope else ""
        bang = " (BREAKING)" if c.breaking else ""
        grouped[section].append(f"- {scope_prefix}{c.summary}{bang}")
    if not grouped:
        return ""
    order = [
        "### Features", "### Bug Fixes", "### Performance",
        "### Security", "### Refactoring", "### Reverts",
    ]
    out: list[str] = []
    for section in order:
        items = grouped.get(section)
        if items:
            out.append(section)
            out.extend(items)
            out.append("")
    return "\n".join(out).rstrip() + "\n"
