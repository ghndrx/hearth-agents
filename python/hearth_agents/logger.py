"""Structured logging configured once at import.

Emits to stdout AND to ``${LOG_FILE:-/tmp/hearth-agents.log}`` so the self-tune
feature can ``read_file`` the agent's own log. Without a stable, readable path
the self-improvement loop has nothing to reflect on.
"""

import logging
import os
import sys
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging for console + file output."""
    log_file = os.environ.get("LOG_FILE", "/tmp/hearth-agents.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        # If the log path isn't writable, fall back to stdout only rather than
        # crashing startup. Self-tune will just have less to read.
        pass

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


configure_logging()
log = structlog.get_logger("hearth")
