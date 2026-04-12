"""Entry point — runs the HTTP server, the autonomous loop, and the Telegram bot concurrently.

All three share one ``Backlog`` instance + one DeepAgent instance, so state stays
consistent without any IPC. ``asyncio.gather`` propagates cancellation correctly
on Ctrl-C and on container shutdown.
"""

from __future__ import annotations

import asyncio

import uvicorn

from .agent import build_agent
from .backlog import Backlog
from .bot import run_bot
from .config import settings
from .logger import log
from .loop import run_forever
from .server import build_app


async def _serve(app) -> None:  # type: ignore[no-untyped-def]
    config = uvicorn.Config(app, host=settings.server_host, port=settings.server_port, log_config=None)
    await uvicorn.Server(config).serve()


async def _main() -> None:
    backlog = Backlog(settings.backlog_path)
    agent = build_agent()
    app = build_app(backlog, agent)
    log.info("starting", port=settings.server_port, stats=backlog.stats())

    await asyncio.gather(
        _serve(app),
        run_forever(backlog, agent),
        run_bot(backlog, agent),
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
