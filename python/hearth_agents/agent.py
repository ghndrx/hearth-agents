"""Top-level DeepAgent — the orchestrator that delegates to subagents.

``create_deep_agent`` gives us planning (``write_todos``), a virtual filesystem
(``read_file`` / ``write_file`` / ``edit_file`` / ``ls``), and subagent spawning
out of the box. We add wikidelve + git + web_search on top.
"""

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from .config import settings
from .models import build_kimi, build_minimax
from .prompts import ORCHESTRATOR_INSTRUCTIONS
from .subagents import build_subagents
from .tools import (
    git_branch_create,
    git_commit,
    git_push,
    git_status,
    git_worktree_add,
    git_worktree_remove,
    record_planner_estimate,
    repo_reindex,
    repo_search,
    run_command,
    web_search,
    wikidelve_pending_jobs,
    wikidelve_read,
    wikidelve_recent_completions,
    wikidelve_research,
    wikidelve_search,
)

ORCHESTRATOR_TOOLS = [
    wikidelve_search,
    wikidelve_read,
    wikidelve_research,
    wikidelve_pending_jobs,
    wikidelve_recent_completions,
    repo_search,
    repo_reindex,
    web_search,
    run_command,
    git_status,
    git_commit,
    git_push,
    git_branch_create,
    git_worktree_add,
    git_worktree_remove,
    record_planner_estimate,
]


def _build_with_model(model):  # type: ignore[no-untyped-def]
    """Construct a DeepAgent bound to the given model. Both Kimi and MiniMax
    agents share the same tools, subagents, and filesystem backend — only the
    underlying chat model differs."""
    fs_root = Path(settings.hearth_repo_path).resolve().parent
    return create_deep_agent(
        tools=ORCHESTRATOR_TOOLS,
        system_prompt=ORCHESTRATOR_INSTRUCTIONS,
        subagents=build_subagents(),
        model=model,
        backend=FilesystemBackend(root_dir=fs_root, virtual_mode=False),
        debug=True,
    )


def build_agent():
    """Create the primary (Kimi) DeepAgent.

    Kimi is the strongest coder on our stack (76.8% SWE-Bench). Used by
    default for everything; falls back to ``build_fallback_agent`` only when
    Kimi rate-limits.
    """
    return _build_with_model(build_kimi())


def build_fallback_agent():
    """Create the fallback (MiniMax) DeepAgent for when Kimi 429s.

    MiniMax M2.7 is weaker at code generation but has a separate quota bucket
    (Plus plan: 4500 req/5hr), so when Kimi's window saturates we keep shipping
    features instead of sleeping.
    """
    return _build_with_model(build_minimax())


_KANBAN_SYSTEM_PROMPT = """You are the hearth-agents chat operator.

You manage an autonomous SDLC backlog through a set of tools. Users chat
with you naturally ("nuke all the gh-* features", "what's costing the
most?", "approve everything blocked on test failures") and you translate
that into the right tool calls.

Rules:
  1. Before acting on "everything matching X", call kanban_list first
     to get the actual feature IDs. Confirm counts if the action is
     destructive (nuke, fresh_retry).
  2. Prefer the query DSL over broad unfiltered listings. Examples:
       status:blocked AND heal_attempts>=2
       status:blocked AND name:CVE
       kind:bug AND status:pending
  3. For every destructive action (nuke, fresh_retry) report the
     feature IDs BEFORE calling the tool, then call the tool, then
     report the result. Don't do silent batch destruction.
  4. When a user reports a bug in chat, use kanban_queue with
     kind=bug and a real repro_command (or ask for one). Do not
     invent repro_commands.
  5. Short replies. Operator is on Telegram; walls of text are
     unwelcome. Truncate lists past 10 rows with a count.
"""


def build_kanban_agent():
    """Slim agent backing the Telegram freeform handler. Has ONLY the
    kanban-ops tools, not the full worktree/git/wikidelve surface —
    chat operators need backlog management, not code generation.
    Cheaper per message too."""
    from .tools.kanban_ops import (
        kanban_act,
        kanban_cost,
        kanban_dashboard,
        kanban_health,
        kanban_list,
        kanban_queue,
        kanban_show,
        kanban_stats,
    )
    return create_deep_agent(
        tools=[kanban_list, kanban_act, kanban_queue, kanban_show,
               kanban_stats, kanban_cost, kanban_health, kanban_dashboard],
        system_prompt=_KANBAN_SYSTEM_PROMPT,
        subagents=[],
        model=build_minimax(),  # cheap model; chat ops don't need Kimi
        backend=FilesystemBackend(root_dir=Path("/tmp").resolve(), virtual_mode=True),
        debug=False,
    )
