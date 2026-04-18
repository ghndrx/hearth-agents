"""Entry point — runs the HTTP server, the autonomous loop, and the Telegram bot concurrently.

All three share one ``Backlog`` instance + one DeepAgent instance, so state stays
consistent without any IPC. ``asyncio.gather`` propagates cancellation correctly
on Ctrl-C and on container shutdown.
"""

from __future__ import annotations

import asyncio

import uvicorn

from .agent import build_agent, build_fallback_agent
from .backlog import Backlog
from .bot import run_bot
from .config import settings
from .archive_task import run_archive
from .digest import run_digest
from .drift_alarm import run_drift_alarm
from .scheduler import run_scheduler
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
    log.info("starting", port=settings.server_port, stats=backlog.stats())

    await asyncio.gather(
        _serve(app),
        run_forever(backlog, agent, fallback_agent=fallback_agent),
        run_bot(backlog, agent),
        run_idea_engine(backlog),
        run_healer(backlog),
        run_worktree_gc(backlog),
        run_digest(backlog),
        run_drift_alarm(),
        run_archive(backlog),
        run_scheduler(backlog),
        run_stuck_feature_escalator(backlog),
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
