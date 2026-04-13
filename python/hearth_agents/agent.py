"""Top-level DeepAgent — the orchestrator that delegates to subagents.

``create_deep_agent`` gives us planning (``write_todos``), a virtual filesystem
(``read_file`` / ``write_file`` / ``edit_file`` / ``ls``), and subagent spawning
out of the box. We add wikidelve + git + web_search on top.
"""

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from .config import settings
from .models import build_kimi
from .prompts import ORCHESTRATOR_INSTRUCTIONS
from .subagents import build_subagents
from .tools import (
    git_branch_create,
    git_commit,
    git_status,
    git_worktree_add,
    git_worktree_remove,
    run_command,
    web_search,
    wikidelve_read,
    wikidelve_research,
    wikidelve_search,
)

ORCHESTRATOR_TOOLS = [
    wikidelve_search,
    wikidelve_read,
    wikidelve_research,
    web_search,
    run_command,
    git_status,
    git_commit,
    git_branch_create,
    git_worktree_add,
    git_worktree_remove,
]


def build_agent():
    """Create the top-level DeepAgent with all tools and subagents wired in.

    Uses Kimi as the driving model — it's the strongest coder on our stack
    (76.8% SWE-Bench) and the orchestrator's job is mostly code reasoning plus
    delegation, which plays to Kimi's strengths.
    """
    # Root the filesystem backend at the parent of all target repos so the
    # agent can ``ls``/``read_file``/``write_file`` against real Hearth code
    # (and against hearth-agents itself for dogfooding). Without this override,
    # DeepAgents defaults to a virtual in-memory FS that can't see real files.
    fs_root = Path(settings.hearth_repo_path).resolve().parent
    return create_deep_agent(
        tools=ORCHESTRATOR_TOOLS,
        system_prompt=ORCHESTRATOR_INSTRUCTIONS,
        subagents=build_subagents(),
        model=build_kimi(),
        backend=FilesystemBackend(root_dir=fs_root, virtual_mode=False),
        debug=True,
    )
