"""structlog wiring: one JSONL file per run, plus human-readable stderr.

The JSONL file is the wire format for the UI's live console - the SSE endpoint
tails it and forwards each line verbatim. That means:

* one JSON object per line, no pretty printing, no multi-line tracebacks;
* an ``ts`` field in ISO-8601 UTC and a ``level`` field on every line, because
  the client filters on them;
* ``run_id`` and ``video_id`` bound via contextvars so every line inside a
  worker task carries them without being passed down manually.

The file is opened line-buffered and flushed per record. A four-hour run whose
log only lands on disk at exit is useless.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, TextIO

import structlog

LOG_SCHEMA_VERSION: Final = 1

_LEVELS: Final[dict[str, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_jsonl_handle: TextIO | None = None


def run_log_path(log_dir: Path, run_id: int) -> Path:
    return log_dir / f"run-{run_id}.jsonl"


def configure(
    *,
    level: str = "info",
    jsonl_path: Path | None = None,
    stderr: bool = True,
    force_json_stderr: bool = False,
) -> None:
    """Configure structlog process-wide. Safe to call once per process.

    Args:
        level: Minimum level to emit.
        jsonl_path: If given, every record is also appended here as JSON.
        stderr: Emit to stderr as well.
        force_json_stderr: Use JSON on stderr instead of the console renderer.
            The API sets this when it captures a subprocess's stderr.
    """
    global _jsonl_handle

    min_level = _LEVELS.get(level.lower(), logging.INFO)

    if _jsonl_handle is not None:
        try:
            _jsonl_handle.close()
        finally:
            _jsonl_handle = None

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _jsonl_handle = jsonl_path.open("a", encoding="utf-8", buffering=1)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        _shorten_event,
    ]

    processors: list[structlog.typing.Processor] = [
        *shared,
        structlog.processors.format_exc_info,
        _JsonlTee(),
    ]
    if stderr:
        if force_json_stderr:
            processors.append(structlog.processors.JSONRenderer(sort_keys=False))
        else:
            processors.append(
                structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
            )
    else:
        processors.append(_drop_everything)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (SQLAlchemy, urllib3, uvicorn) through the same
    # minimum level so a DEBUG-chatty dependency cannot flood the JSONL file.
    logging.basicConfig(level=max(min_level, logging.WARNING), stream=sys.stderr)


def _shorten_event(
    _logger: object, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Keep a single log line small enough to stream comfortably."""
    for key in ("error_message", "event"):
        value = event_dict.get(key)
        if isinstance(value, str) and len(value) > 2000:
            event_dict[key] = value[:2000] + "...[truncated]"
    return event_dict


def _drop_everything(
    _logger: object, _name: str, _event_dict: structlog.typing.EventDict
) -> str:
    raise structlog.DropEvent


class _JsonlTee:
    """Append each record to the run's JSONL file, then pass it along."""

    def __init__(self) -> None:
        self._render = structlog.processors.JSONRenderer(sort_keys=False)

    def __call__(
        self, logger: object, name: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        handle = _jsonl_handle
        if handle is not None:
            line = self._render(logger, name, dict(event_dict))
            try:
                handle.write(str(line) + "\n")
                handle.flush()
            except (OSError, ValueError):
                # A full or unlinked log disk must never take down a run.
                pass
        return event_dict


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind(**kwargs: Any) -> None:
    """Bind context for this thread/task: ``bind(run_id=7, video_id="abc")``."""
    structlog.contextvars.bind_contextvars(**kwargs)


def unbind(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


@contextmanager
def context(**kwargs: Any) -> Iterator[None]:
    """Temporarily bind context, restoring the previous values on exit."""
    tokens = structlog.contextvars.bind_contextvars(**kwargs)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def close() -> None:
    """Flush and close the JSONL handle. Call on clean shutdown."""
    global _jsonl_handle
    if _jsonl_handle is not None:
        try:
            _jsonl_handle.flush()
            os.fsync(_jsonl_handle.fileno())
        except (OSError, ValueError):
            pass
        finally:
            try:
                _jsonl_handle.close()
            finally:
                _jsonl_handle = None
