"""Structured logging + per-request UUID context.

Logging requirement: every log line must capture a timestamp,
a severity level, the service name, and the request/user UUID.

We solve this with two pieces:
  1. A configured logger whose formatter always emits
     timestamp | level | service-name | [uuid] | message.
  2. A FastAPI middleware that resolves the UUID (from the ?uuid= query param,
     falling back to a fresh one) and stashes it in a ContextVar. The log
     formatter reads that ContextVar on every record, so all logs emitted while
     a request is in flight automatically carry the right uuid.

OPS-REQ-1 / OPS-API-2: we also keep a bounded, in-memory ring of recent core-API
usage so OPS-API-2 can report the number of distinct users in the last 30 seconds.
NOTE: this is per-pod state. Across multiple replicas each pod only sees its own
share of traffic, so OPS-API-2 under-counts cluster-wide. See README.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid as uuid_mod

SERVICE_NAME = "smartpark-api"

current_uuid: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_uuid", default="-"
)

RECENT_REQUEST_LOG: list[dict] = []  # {"ts": float, "uuid": str, "endpoint": str}
RECENT_REQUEST_LOG_WINDOW_S = 30.0
RECENT_REQUEST_LOG_MAX = 10_000


def _make_logger() -> logging.Logger:
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)

    class _UuidFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            uid = current_uuid.get()
            record.msg = f"[{uid}] {record.getMessage()}"
            record.args = ()
            return super().format(record)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            _UuidFormatter(
                fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger


log = _make_logger()


def resolve_uuid(raw: str | None) -> str:
    """Return a sanitised uuid from the query string, or manufacture one."""
    if raw:
        candidate = raw.strip()
        if 1 <= len(candidate) <= 64:
            return candidate
    return str(uuid_mod.uuid4())


def log_request(ts: float, uid: str, endpoint: str) -> None:
    """Record a core-API call for OPS-API-2 (last-30s user count)."""
    RECENT_REQUEST_LOG.append({"ts": ts, "uuid": uid, "endpoint": endpoint})
    _prune_recent(ts)


def count_unique_users(seconds: float = RECENT_REQUEST_LOG_WINDOW_S) -> int:
    """Count distinct user uuids that hit a core API within the last window."""
    now = time.time()
    cutoff = now - seconds
    return len({entry["uuid"] for entry in RECENT_REQUEST_LOG if entry["ts"] >= cutoff})


def _prune_recent(now: float) -> None:
    """Drop entries older than the window (and enforce a hard size cap)."""
    cutoff = now - RECENT_REQUEST_LOG_WINDOW_S
    RECENT_REQUEST_LOG[:] = [e for e in RECENT_REQUEST_LOG if e["ts"] >= cutoff]
    if len(RECENT_REQUEST_LOG) > RECENT_REQUEST_LOG_MAX:
        del RECENT_REQUEST_LOG[: len(RECENT_REQUEST_LOG) - RECENT_REQUEST_LOG_MAX]
