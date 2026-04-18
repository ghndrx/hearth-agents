"""i18n hardcoded-string detector (research #3839 — autonomous i18n).

Scans a source file for user-facing literal strings that should be
behind a translation key. Emits a missing-keys report the agent can
use to drive the extraction refactor.

This is a lint, not a transform — deliberately so. The article is
explicit that nuance-critical strings need human review before
translation generation, so we flag rather than auto-translate.
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import tool

# Heuristics per language. Intentionally conservative — false positives
# are annoying; false negatives get caught at review.
_JSX_TEXT_NODE = re.compile(r">\s*([A-Z][a-zA-Z ]{4,}[.!?]?)\s*<")  # JSX >Hello World<
_JSX_ATTR = re.compile(r'(?:aria-label|title|placeholder|alt)="([^"]{3,})"')
_SVELTE_TEXT = re.compile(r">\s*([A-Z][a-zA-Z ]{4,}[.!?]?)\s*<")
_GO_STRING = re.compile(r'"([A-Z][a-zA-Z ]{4,}[.!?]?)"')  # display-string-ish


@tool
def scaffold_i18n(file_path: str) -> str:
    """Scan ``file_path`` for hardcoded user-facing strings that probably
    need a translation key. Returns a report with line:literal pairs
    plus a suggested key name for each.

    Use this on frontend files before shipping a feature that adds
    copy. Research #3839 flags ~20% of "ready" frontend PRs regress
    i18n without this check.
    """
    path = Path(file_path)
    if not path.exists():
        return f"error: {file_path} not found"
    suffix = path.suffix.lower()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"error reading: {e}"
    findings: list[str] = []

    def _slug(s: str) -> str:
        return "_".join(w.lower() for w in re.findall(r"[A-Za-z]+", s))[:40]

    patterns = []
    if suffix in (".jsx", ".tsx", ".svelte", ".vue", ".html"):
        patterns.extend([_JSX_TEXT_NODE, _JSX_ATTR, _SVELTE_TEXT])
    elif suffix == ".go":
        patterns.append(_GO_STRING)
    else:
        return f"unsupported: {suffix}; i18n scan runs on .jsx/.tsx/.svelte/.vue/.html/.go"
    for i, line in enumerate(content.splitlines(), start=1):
        for pat in patterns:
            for m in pat.finditer(line):
                literal = m.group(1).strip()
                if literal.lower() in ("null", "undefined", "true", "false"):
                    continue
                findings.append(f"  {file_path}:{i} → literal {literal!r} — suggested key: t('{_slug(literal)}')")
    if not findings:
        return f"(no hardcoded strings detected in {file_path})"
    return (
        f"{len(findings)} hardcoded-string candidate(s) in {file_path}:\n"
        + "\n".join(findings[:40])
        + ("\n... (truncated)" if len(findings) > 40 else "")
    )
