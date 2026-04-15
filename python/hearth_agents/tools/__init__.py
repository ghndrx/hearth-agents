"""Custom tools the DeepAgent calls in addition to built-in filesystem/shell tools."""

from .git_ops import git_branch_create, git_commit, git_status, git_worktree_add, git_worktree_remove
from .planner_tools import record_planner_estimate
from .repo_search import repo_reindex, repo_search
from .serper import web_search
from .shell import run_command
from .wikidelve import (
    wikidelve_pending_jobs,
    wikidelve_read,
    wikidelve_recent_completions,
    wikidelve_research,
    wikidelve_search,
)

__all__ = [
    "wikidelve_search",
    "wikidelve_read",
    "wikidelve_research",
    "wikidelve_pending_jobs",
    "wikidelve_recent_completions",
    "repo_search",
    "repo_reindex",
    "web_search",
    "run_command",
    "git_status",
    "git_commit",
    "git_branch_create",
    "git_worktree_add",
    "git_worktree_remove",
    "record_planner_estimate",
]
