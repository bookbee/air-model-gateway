from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from air_model_gateway.config import Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log.level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.log.render == "json":
        processors += [add_severity, structlog.processors.JSONRenderer()]
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def add_severity(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Copy `level` into `severity`, the field Cloud Logging (and most log
    agents) read a JSON line's severity from. Without it every line from a GKE
    pod lands as DEFAULT, so an error can't be filtered or alerted on by level.
    `level` is kept for anyone already querying on it."""
    level = event_dict.get("level")
    if isinstance(level, str):
        event_dict["severity"] = level.upper()
    return event_dict


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
