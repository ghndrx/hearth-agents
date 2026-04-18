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
from .digest import run_digest
from .drift_alarm import run_drift_alarm
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


async def _main() -> None:
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
