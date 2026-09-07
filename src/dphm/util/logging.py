"""structlog JSON to stdout, run_id on every line (12 §8).

The state store is the durable record; logs are for operating the service, not for
answering questions about data. Anything that must be answerable later goes in
DPHM_STATE (13 §6).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False


def configure(*, level: str = "INFO", json_output: bool = True) -> None:
    global _configured
    if _configured:
        return

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_run(run_id: str, **extra: Any) -> None:
    """Bind a run to the context so every subsequent line carries it."""
    structlog.contextvars.bind_contextvars(run_id=run_id, **extra)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()
