"""Entry point — runs the HTTP server, the autonomous loop, and the Telegram bot concurrently.

All three share one ``Backlog`` instance + one DeepAgent instance, so state stays
consistent without any IPC. ``asyncio.gather`` propagates cancellation correctly
on Ctrl-C and on container shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import uvicorn

from .agent import build_agent, build_fallback_agent
from .backlog import Backlog
from .bot import run_bot
from .config import settings
from .archive_task import run_archive
from .budget_alarm import run_budget_alarm
from .drift_canary import run_drift_canary
from .digest import run_digest
from .drift_alarm import run_drift_alarm
from .nightly_summary import run_nightly_summary
from .research_watch import run_research_watch
from .transition_compaction import run_transition_compaction
from .scheduler import run_scheduler
from .self_improvement_seeder import run_self_improvement_seeder
from .snapshot_task import run_snapshot
from .stuck_feature_escalator import run_stuck_feature_escalator
from .gc_worktrees import run_worktree_gc
from .healer import run_healer
from .idea_engine import run_idea_engine
from .logger import log
from .loop import run_forever
from .server import build_app


async def _serve(app) -> None:  # type: ignore[no-untyped-def]
    config = uvicorn.Config(app, host=settings.server_host, port=settings.server_port, log_config=None)
    await uvicorn.Server(config).serve()


def _normalize_primary_repos() -> None:
    """Reset each primary repo to its base branch on a clean tree.

    Rationale: tools like ``git_branch_create`` used to checkout feature
    branches in the primary repo, leaving untracked files (e.g. a stale
    ``internal/matrixfederation/`` directory from an abandoned run) that
    then broke ``go build`` for every other worker that cd'd into the
    primary. Now guarded at the tool level, but existing state still
    needs cleanup. Runs once per process start; best-effort — skip if
    git fetch fails (offline dev) rather than block boot.
    """
    import subprocess
    for repo_name, repo_path in settings.repo_paths.items():
        try:
            subprocess.run(
                ["git", "-C", repo_path, "fetch", "origin", "develop", "--depth", "1"],
                capture_output=True, timeout=15, check=False,
            )
            subprocess.run(
                ["git", "-C", repo_path, "checkout", "develop"],
                capture_output=True, timeout=10, check=False,
            )
            subprocess.run(
                ["git", "-C", repo_path, "reset", "--hard", "origin/develop"],
                capture_output=True, timeout=10, check=False,
            )
            # Purge untracked cruft left behind by aborted feature runs. Scoped
            # to the primary only; worktrees under /repos/worktrees-* are not
            # touched because `-C repo_path` stays inside the primary repo.
            subprocess.run(
                ["git", "-C", repo_path, "clean", "-fd"],
                capture_output=True, timeout=10, check=False,
            )
            log.info("primary_repo_normalized", repo=repo_name, path=repo_path)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("primary_repo_normalize_failed", repo=repo_name, error=str(e)[:200])


async def _main() -> None:
    _normalize_primary_repos()
    backlog = Backlog(settings.backlog_path)
    agent = build_agent()
    # Build the fallback eagerly so the first 429 doesn't pay model-init latency
    # (and so config errors surface at startup, not mid-feature).
    try:
        fallback_agent = build_fallback_agent()
        log.info("fallback_agent_ready")
    except Exception as e:
        fallback_agent = None
        log.warning("fallback_agent_unavailable", error=str(e))
    app = build_app(backlog, agent)
    # Expose fallback_agent on app.state so endpoints (e.g. /debate) can
    # reach the second model without reaching into module-level globals.
    app.state.fallback_agent = fallback_agent
    # Background-task registry for /admin/restart-task. Each entry is
    # (current_task, factory_callable) so the endpoint can cancel and
    # re-spawn from the same factory.
    factories = {
        "loop": lambda: run_forever(backlog, agent, fallback_agent=fallback_agent),
        "bot": lambda: run_bot(backlog, agent),
        "idea_engine": lambda: run_idea_engine(backlog),
        "healer": lambda: run_healer(backlog),
        "worktree_gc": lambda: run_worktree_gc(backlog),
        "digest": lambda: run_digest(backlog),
        "drift_alarm": lambda: run_drift_alarm(),
        "archive": lambda: run_archive(backlog),
        "scheduler": lambda: run_scheduler(backlog),
        "stuck_feature_escalator": lambda: run_stuck_feature_escalator(backlog),
        "self_improvement_seeder": lambda: run_self_improvement_seeder(backlog),
        "snapshot": lambda: run_snapshot(backlog),
        "research_watch": lambda: run_research_watch(),
        "nightly_summary": lambda: run_nightly_summary(backlog),
        "transition_compaction": lambda: run_transition_compaction(),
        "budget_alarm": lambda: run_budget_alarm(),
        "drift_canary": lambda: run_drift_canary(),
    }
    bg_tasks: dict[str, tuple[asyncio.Task, Any]] = {}
    for name, factory in factories.items():
        t = asyncio.create_task(factory())
        bg_tasks[name] = (t, factory)
    app.state.background_tasks = bg_tasks
    log.info("starting", port=settings.server_port, stats=backlog.stats())

    # gather() the server alongside the registered background tasks; each
    # task's lifecycle is owned by the registry so /admin/restart-task can
    # replace any of them without disturbing the others.
    await asyncio.gather(
        _serve(app),
        *[entry[0] for entry in bg_tasks.values()],
        return_exceptions=True,
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
