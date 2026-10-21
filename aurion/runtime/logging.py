"""Structured JSON logging with rotation. `docs/01-architecture.md` §7.

One line per event with ``instrument``, ``as_of``, ``event``, ``decision`` and
``rejected_reason``, because the candidate funnel is only auditable if the rejections are
machine-readable.

Secrets are redacted on the way out. A bot token in a log line is a leaked token, and the
most likely way one gets there is an exception message that happens to contain a URL.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from aurion.core.timeframe import UTC

#: Anything shaped like a Telegram token, an OANDA bearer token or a FRED key.
_SECRET_PATTERNS = (
    re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)(api_key=)[A-Za-z0-9]{16,}"),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
)

_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    }
)


def redact(text: str) -> str:
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", out)
    return out


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            try:
                json.dumps(value, default=str)
            except (TypeError, ValueError):  # pragma: no cover
                value = str(value)
            payload[key] = value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, sort_keys=True)


def configure(
    level: str = "INFO",
    log_dir: str | Path | None = None,
    rotate_mb: int = 50,
    backups: int = 5,
    json_format: bool = True,
) -> None:
    """Install handlers. Idempotent, so calling it twice does not double every line."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter: logging.Formatter = (
        JsonFormatter()
        if json_format
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path / "aurion.log", maxBytes=rotate_mb * 1024 * 1024, backupCount=backups
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    # httpx logs every request at INFO, which on a streaming adapter is one line a second.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_candidate(
    logger: logging.Logger,
    instrument: str,
    as_of: datetime,
    step: str,
    reason: str | None,
    **extra: Any,
) -> None:
    """One structured line per candidate, accepted or rejected.

    An emission is INFO; a rejection is DEBUG. Every evaluation logged at INFO is ~17,000
    lines for a 180-day replay, all of them `bias:htf_structure_unavailable` while the
    warm-up elapses, which buries the startup warnings and the alerts that matter. The
    counts are not lost: `HealthMonitor.counters.rejections_by_reason` aggregates them and
    the heartbeat and `/status` report the breakdown.
    """
    level = logging.INFO if reason is None else logging.DEBUG
    logger.log(
        level,
        "candidate",
        extra={
            "instrument": instrument,
            "as_of": as_of.isoformat(),
            "event": "candidate",
            "decision": "emitted" if reason is None else "rejected",
            "step": step,
            "rejected_reason": reason or "",
            **extra,
        },
    )


def env_level(default: str = "INFO") -> str:
    return os.environ.get("AURION_LOG_LEVEL", default)
