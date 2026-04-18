"""Acceptance-criteria validator (research #3836 — Autonomous PO).

Research prescribes an INCOSE-style gate BEFORE a PRD / feature goes
into implementation: every acceptance criterion must be testable,
unambiguous, and consistent. Without it, AI-generated requirements
"multiply hidden defects."

Lightweight, prompt-free implementation: static checks against the
Feature.acceptance_criteria string. The agent calls this after
writing a draft and rewrites on failure. Fast failure beats slow
re-discovery downstream.
"""

from __future__ import annotations

from langchain_core.tools import tool


_AMBIGUOUS_WORDS = (
    " some ", " maybe ", " could ", " might ", " reasonable",
    " proper ", " appropriate ", " sufficient ", " good ",
    " nice ", " as needed", " later",
)

_TESTABILITY_HINTS = (
    " when ", " then ", " exits 0", " returns ", " http 2", " http 4", " http 5",
    " count ", " equals", " matches", " contains", " rows", " ms", " seconds",
    " fails ", " passes", " response ", " status ",
)


@tool
def validate_acceptance_criteria(acceptance_criteria: str) -> str:
    """Check an acceptance_criteria string against INCOSE-style rules:
    testable, unambiguous, concise. Returns "OK" or a list of issues.

    Call this BEFORE POST /features when queuing work from unstructured
    user input (Slack, support ticket, loose feature request). If it
    fails, rewrite the criterion to include concrete success signals
    (exact command, exact HTTP response, exact test that passes).
    """
    if not acceptance_criteria or not acceptance_criteria.strip():
        return "FAIL: acceptance_criteria is empty — every feature needs a concrete done condition."
    text = " " + acceptance_criteria.strip().lower() + " "
    issues: list[str] = []
    ambiguous = [w.strip() for w in _AMBIGUOUS_WORDS if w in text]
    if ambiguous:
        issues.append(
            f"AMBIGUOUS: contains vague word(s) {ambiguous}. "
            "Replace with concrete signals (exact command output, HTTP status, test name)."
        )
    if not any(h in text for h in _TESTABILITY_HINTS):
        issues.append(
            "NOT_TESTABLE: no observable success signal. Add one of: "
            "exact command + expected exit code, HTTP request + expected status, "
            "test file + test name that passes, database row count."
        )
    if len(acceptance_criteria) > 600:
        issues.append(
            "TOO_LONG: >600 chars. Multiple criteria in one line is a smell — "
            "split into separate Features or use depends_on."
        )
    if not issues:
        return "OK"
    return "FAIL:\n  " + "\n  ".join(issues)
