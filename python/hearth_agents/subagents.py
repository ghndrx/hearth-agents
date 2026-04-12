"""Subagent definitions for the DeepAgent orchestrator.

DeepAgents spawns these as isolated sub-graphs. Each gets its own system prompt
and its own tool subset — the orchestrator only sees the final summary, which
keeps the top-level loop from drowning in implementation noise.
"""

from typing import Any

from .prompts import (
    DEVELOPER_INSTRUCTIONS,
    PLANNER_INSTRUCTIONS,
    REVIEWER_INSTRUCTIONS,
    SECURITY_INSTRUCTIONS,
)
from .tools import (
    git_commit,
    git_status,
    web_search,
    wikidelve_read,
    wikidelve_search,
)


def build_subagents() -> list[dict[str, Any]]:
    """Return the SubAgent specs expected by ``create_deep_agent``.

    Keyed by ``name``/``description``/``system_prompt``/``tools`` — these are
    the field names DeepAgents' ``SubAgent`` TypedDict requires.
    """
    return [
        {
            "name": "planner",
            "description": (
                "Breaks a feature into an ordered, verifiable implementation plan. "
                "Use before delegating to ``developer`` when the feature spans >3 files."
            ),
            "system_prompt": PLANNER_INSTRUCTIONS,
            "tools": [wikidelve_search, wikidelve_read, web_search],
        },
        {
            "name": "developer",
            "description": (
                "Writes production code in an isolated worktree. Must be given the "
                "worktree path and a concrete task list."
            ),
            "system_prompt": DEVELOPER_INSTRUCTIONS,
            "tools": [wikidelve_search, wikidelve_read, web_search, git_status, git_commit],
        },
        {
            "name": "reviewer",
            "description": (
                "Reviews a developer's diff against acceptance criteria. Returns a "
                "JSON verdict (APPROVE / REQUEST_CHANGES / BLOCK)."
            ),
            "system_prompt": REVIEWER_INSTRUCTIONS,
            "tools": [git_status, wikidelve_search, wikidelve_read],
        },
        {
            "name": "security",
            "description": (
                "Security review: OWASP, E2EE correctness, dependency CVEs. Call for "
                "any change touching auth, crypto, or external input."
            ),
            "system_prompt": SECURITY_INSTRUCTIONS,
            "tools": [git_status, wikidelve_search, wikidelve_read, web_search],
        },
    ]
