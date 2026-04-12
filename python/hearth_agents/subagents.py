"""Subagent definitions for the DeepAgent orchestrator.

DeepAgents spawns these as isolated sub-graphs. Each gets its own system prompt,
its own tool subset, and its own context window — the orchestrator only sees the
final summary, which keeps the top-level loop from drowning in implementation noise.
"""

from typing import Any

from .prompts import (
    DEVELOPER_INSTRUCTIONS,
    PLANNER_INSTRUCTIONS,
    REVIEWER_INSTRUCTIONS,
    SECURITY_INSTRUCTIONS,
)


# Tool names are strings because DeepAgents resolves them against the tool list
# passed to ``create_deep_agent``. Keeping this as data (not imports) means we
# can change which tools each subagent can reach without touching agent.py.
SUBAGENTS: list[dict[str, Any]] = [
    {
        "name": "planner",
        "description": (
            "Breaks a feature into an ordered, verifiable implementation plan. "
            "Use before delegating to ``developer`` when the feature spans >3 files "
            "or touches unfamiliar subsystems."
        ),
        "prompt": PLANNER_INSTRUCTIONS,
        "tools": ["wikidelve_search", "wikidelve_read", "web_search"],
    },
    {
        "name": "developer",
        "description": (
            "Writes production code in an isolated worktree. Must be given the "
            "worktree path and a concrete task list. Returns a summary of files "
            "changed and the commit SHA."
        ),
        "prompt": DEVELOPER_INSTRUCTIONS,
        "tools": [
            "wikidelve_search",
            "wikidelve_read",
            "web_search",
            "git_status",
            "git_commit",
        ],
    },
    {
        "name": "reviewer",
        "description": (
            "Reviews a developer's diff against the feature's acceptance criteria. "
            "Returns a structured JSON verdict (APPROVE / REQUEST_CHANGES / BLOCK)."
        ),
        "prompt": REVIEWER_INSTRUCTIONS,
        "tools": ["git_status", "wikidelve_search", "wikidelve_read"],
    },
    {
        "name": "security",
        "description": (
            "Security-focused review: OWASP, E2EE correctness, dependency CVEs. "
            "Call for any change touching auth, crypto, or external input."
        ),
        "prompt": SECURITY_INSTRUCTIONS,
        "tools": ["git_status", "wikidelve_search", "wikidelve_read", "web_search"],
    },
]
