"""Structured logging + per-request UUID context.
结构化日志 + 每个请求的 UUID 上下文.

Logging requirement (assignment 4.3): every log line must capture a timestamp,
a severity level, the service name, and the request/user UUID.
日志要求(作业 4.3):每行日志都必须包含时间戳,级别,服务名和请求/用户 UUID.

We solve this with two pieces:
  1. A configured logger whose formatter always emits
     timestamp | level | service-name | [uuid] | message.
  2. A FastAPI middleware that resolves the UUID (from the ?uuid= query param,
     falling back to a fresh one) and stashes it in a ContextVar. The log
     formatter reads that ContextVar on every record, so all logs emitted while
     a request is in flight automatically carry the right uuid.
我们用两部分解决:
  1. 一个配置好的 logger,其 formatter 始终输出
     时间戳 | 级别 | 服务名 | [uuid] | 消息.
  2. 一个 FastAPI 中间件,解析 UUID(从 ?uuid= 查询参数,否则生成一个新的),
     并存入 ContextVar.日志 formatter 在每条记录上读取该 ContextVar,
     因此请求处理期间发出的所有日志都会自动带上正确的 uuid.

OPS-REQ-1 / OPS-API-2: we also keep a bounded, in-memory ring of recent core-API
usage so OPS-API-2 can report the number of distinct users in the last 30 seconds.
NOTE: this is per-pod state. Across multiple replicas each pod only sees its own
share of traffic, so OPS-API-2 under-counts cluster-wide. See README.
OPS-REQ-1 / OPS-API-2:我们还维护一个有界的,内存中的最近核心 API 使用环形记录,
使 OPS-API-2 能报告最近 30 秒内的不同用户数.
注意:这是每个 Pod 的状态.在多副本下,每个 Pod 只看到自己那份流量,
因此 OPS-API-2 会在集群范围内少计.详见 README.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid as uuid_mod

SERVICE_NAME = "smartpark-api"

# Holds the current request's UUID for the logging formatter to pick up.
# 保存当前请求的 UUID,供日志 formatter 读取.
current_uuid: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_uuid", default="-"
)

# A bounded in-memory list of recent core-API requests for OPS-API-2.
# 一个有界的,内存中的最近核心 API 请求列表,供 OPS-API-2 使用.
RECENT_REQUEST_LOG: list[dict] = []  # {"ts": float, "uuid": str, "endpoint": str}
RECENT_REQUEST_LOG_WINDOW_S = 30.0
RECENT_REQUEST_LOG_MAX = 10_000


def _make_logger() -> logging.Logger:
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)

    class _UuidFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            # Pull the ContextVar at format time so it reflects the active request.
            # 在格式化时读取 ContextVar,以反映当前活动请求.
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
        logger.propagate = False  # Avoid duplicate records. 避免重复记录.
    return logger


log = _make_logger()


def resolve_uuid(raw: str | None) -> str:
    """Return a sanitised uuid from the query string, or manufacture one.
    从查询串返回清洗后的 uuid,否则生成一个新的."""
    if raw:
        candidate = raw.strip()
        if 1 <= len(candidate) <= 64:
            return candidate
    return str(uuid_mod.uuid4())


def log_request(ts: float, uid: str, endpoint: str) -> None:
    """Record a core-API call for OPS-API-2 (last-30s user count).
    记录一次核心 API 调用,供 OPS-API-2(最近 30 秒用户数)使用."""
    RECENT_REQUEST_LOG.append({"ts": ts, "uuid": uid, "endpoint": endpoint})
    _prune_recent(ts)


def count_unique_users(seconds: float = RECENT_REQUEST_LOG_WINDOW_S) -> int:
    """Count distinct user uuids that hit a core API within the last window.
    统计最近窗口内访问过核心 API 的不同用户 uuid 数量."""
    now = time.time()
    cutoff = now - seconds
    return len({entry["uuid"] for entry in RECENT_REQUEST_LOG if entry["ts"] >= cutoff})


def _prune_recent(now: float) -> None:
    """Drop entries older than the window (and enforce a hard size cap).
    丢弃超出窗口的记录(并强制限制列表大小)."""
    cutoff = now - RECENT_REQUEST_LOG_WINDOW_S
    RECENT_REQUEST_LOG[:] = [e for e in RECENT_REQUEST_LOG if e["ts"] >= cutoff]
    if len(RECENT_REQUEST_LOG) > RECENT_REQUEST_LOG_MAX:
        del RECENT_REQUEST_LOG[: len(RECENT_REQUEST_LOG) - RECENT_REQUEST_LOG_MAX]
