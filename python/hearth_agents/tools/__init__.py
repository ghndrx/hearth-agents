"""Custom tools the DeepAgent calls in addition to built-in filesystem/shell tools."""

from .git_ops import (
    git_branch_create,
    git_commit,
    git_push,
    git_status,
    git_worktree_add,
    git_worktree_remove,
)
from .bisect_bench import bisect_bench
from .bisect_bench import bisect_bench
from .classify_bump import classify_bump
from .env_profile import env_profile
from .planner_tools import record_planner_estimate
from .repo_search import repo_reindex, repo_search
from .scaffold import scaffold_test_file
from .scaffold_contract_test import scaffold_contract_test
from .scaffold_i18n import scaffold_i18n
from .scaffold_migration import scaffold_migration
from .scaffold_pbt import scaffold_pbt
from .serper import web_search
from .shell import run_command
from .validate_acceptance_criteria import validate_acceptance_criteria
from .verify_staged import verify_staged
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
    "git_push",
    "git_branch_create",
    "git_worktree_add",
    "git_worktree_remove",
    "record_planner_estimate",
    "scaffold_test_file",
    "scaffold_migration",
    "scaffold_pbt",
    "scaffold_contract_test",
    "verify_staged",
    "classify_bump",
    "env_profile",
    "bisect_bench",
    "scaffold_i18n",
    "validate_acceptance_criteria",
]
