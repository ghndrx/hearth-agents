"""Subagent definitions for the DeepAgent orchestrator.

Three implementation specialists (backend-dev for Go, frontend-dev for
SvelteKit/Tauri/RN, developer for Python/infra), one planner, one reviewer,
one security engineer. Specialization is per wikidelve research #474:
generic ``developer`` prompts underperform role-specific ones when domain
constraints (Fiber, pgx, Svelte 5 runes) are spelled out explicitly.
"""

from typing import Any

from .models import build_minimax
from .prompts import (
    BACKEND_DEV_INSTRUCTIONS,
    DEVELOPER_INSTRUCTIONS,
    FRONTEND_DEV_INSTRUCTIONS,
    PLANNER_INSTRUCTIONS,
    REVIEWER_INSTRUCTIONS,
    SECURITY_INSTRUCTIONS,
)
from .tools import (
    git_commit,
    git_status,
    repo_search,
    run_command,
    web_search,
    wikidelve_read,
    wikidelve_search,
)


def build_subagents() -> list[dict[str, Any]]:
    """Return the SubAgent specs expected by ``create_deep_agent``.

    The reviewer and security subagents run on MiniMax while the dev subagents
    run on Kimi (orchestrator's inherited model). Research on architect/verifier
    patterns is explicit: same-model review shares the reasoning patterns that
    produced the original errors, so use different architectures for review.
    """
    dev_tools = [repo_search, wikidelve_search, wikidelve_read, web_search, run_command, git_status, git_commit]
    review_tools = [repo_search, git_status, run_command, wikidelve_search, wikidelve_read]
    # Build once so both reviewer + security share the client / connection pool.
    review_model = build_minimax()

    return [
        {
            "name": "planner",
            "description": (
                "Breaks a feature into an ordered, verifiable implementation plan. "
                "Use before delegating to a dev subagent when the feature spans >3 files."
            ),
            "system_prompt": PLANNER_INSTRUCTIONS,
            "tools": [wikidelve_search, wikidelve_read, web_search],
        },
        {
            "name": "backend-dev",
            "description": (
                "Writes Go backend code for the Hearth server (Fiber, pgx, Redis). "
                "Use for anything under ``backend/`` in the hearth repo."
            ),
            "system_prompt": BACKEND_DEV_INSTRUCTIONS,
            "tools": dev_tools,
        },
        {
            "name": "frontend-dev",
            "description": (
                "Writes SvelteKit (Svelte 5 runes), Tauri, or React Native code. "
                "Use for anything under ``frontend/``, hearth-desktop, or hearth-mobile."
            ),
            "system_prompt": FRONTEND_DEV_INSTRUCTIONS,
            "tools": dev_tools,
        },
        {
            "name": "developer",
            "description": (
                "Writes Python agent-platform, infra, Dockerfile, CI, or docs changes. "
                "Use for anything in hearth-agents itself or non-client infra."
            ),
            "system_prompt": DEVELOPER_INSTRUCTIONS,
            "tools": dev_tools,
        },
        {
            "name": "reviewer",
            "description": (
                "Reviews a dev subagent's diff against acceptance criteria. "
                "Returns JSON verdict (APPROVE / REQUEST_CHANGES / BLOCK). "
                "Runs on MiniMax for architectural diversity vs the Kimi implementer."
            ),
            "system_prompt": REVIEWER_INSTRUCTIONS,
            "tools": review_tools,
            "model": review_model,
        },
        {
            "name": "security",
            "description": (
                "Security review: OWASP, E2EE correctness, dependency CVEs, prompt "
                "injection. Call for any change touching auth, crypto, or external input. "
                "Runs on MiniMax for architectural diversity."
            ),
            "system_prompt": SECURITY_INSTRUCTIONS,
            "tools": [*review_tools, web_search],
            "model": review_model,
        },
    ]
