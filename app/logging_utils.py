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

# ---------------------------------------------------------------------------
# Why a ContextVar?
# 为什么要用 ContextVar?
#
# A FastAPI app serves many requests concurrently as separate asyncio tasks.
# We want every log line emitted during a request to carry THAT request's uuid,
# without threading the uuid through every function and log call.
# FastAPI 以并发的 asyncio 任务来处理多个请求.我们希望某请求处理期间发出的每行日志,
# 都带上该请求自己的 uuid,而不必把 uuid 手工传遍每个函数和每次日志调用.
#
# A plain module global would be clobbered by concurrent requests. A ContextVar
# is scoped to the current async context: each asyncio Task gets its own copy,
# so set() writes only this task's copy and get() reads only this task's copy.
# 普通模块级全局变量会被并发请求互相覆盖.而 ContextVar 以当前异步上下文为作用域:
# 每个 asyncio 任务有自己的一份拷贝,因此 set() 只写入当前任务的那份,
# get() 也只读到当前任务的那份.
#
# set() returns a Token; reset(token) restores the previous value so the uuid
# does not leak into a reused context.
# set() 返回一个 Token;reset(token) 恢复之前的值,以免 uuid 泄漏给被复用的上下文.
#
# NOTE: worker threads (the YOLO thread pool) have their OWN context and do NOT
# automatically inherit this async ContextVar. We do not log with a uuid from
# inside those threads, so this is fine.
# 注意:worker 线程(如 YOLO 线程池)有自己独立的 context,不会自动继承这个异步 ContextVar.
# 我们没有在线程内部记录带 uuid 的日志,所以此处没有问题.
# ---------------------------------------------------------------------------
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
            # get() the ContextVar at format time. The formatter runs synchronously
            # in the same async context as the log call, so it sees the uuid that
            # the middleware set() for this request.
            # 在格式化时 get() 该 ContextVar.由于 formatter 在日志调用所在的同一异步上下文
            # 中同步运行,所以能看到中间件为当前请求 set() 的 uuid.
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


def _coerce_uuid(raw: str | None) -> str | None:
    """Return a cleaned uuid from the query string, or None if absent/invalid.
    从查询串返回清洗后的 uuid;若缺失或非法则返回 None."""
    if raw:
        candidate = raw.strip()
        if 1 <= len(candidate) <= 64:
            return candidate
    return None


def resolve_uuid(raw: str | None) -> str:
    """Raw uuid for echoing back in responses: the provided uuid, or a fresh uuid4.
    用于响应中回显的原始 uuid:优先用客户端提供的 uuid,否则生成一个新的 uuid4."""
    cleaned = _coerce_uuid(raw)
    return cleaned if cleaned is not None else str(uuid_mod.uuid4())


def build_user_id(raw_uuid: str | None, client_ip: str | None) -> str:
    """Grouping identity used for logging and OPS-API-2 user counting.
    用于日志和 OPS-API-2 用户计数的"分组身份".

    Prefer the client-supplied uuid so a user's requests group together. If no
    uuid is given, fall back to the client IP so the SAME user hitting the API
    many times (e.g. 5 annotate calls) still counts as ONE user, not five.
    Finally, if we have neither, use a fresh anonymous id (for logging only).
    优先用客户端提供的 uuid,使同一用户的请求归并为同一身份.若未传 uuid,则回退到客户端 IP,
    这样同一用户多次调用(例如 5 次 annotate)仍只算 1 个用户,而不是 5 个.
    最后,如果两者都没有,则用一个新的匿名 id(仅用于日志追踪)."""
    cleaned = _coerce_uuid(raw_uuid)
    if cleaned:
        return f"user:{cleaned}"
    if client_ip:
        return f"ip:{client_ip}"
    return f"anon:{uuid_mod.uuid4()}"


def get_client_ip(request) -> str:
    """Best-effort real client IP.
    尽力取真实客户端 IP.

    Behind a proxy / load-balancer (e.g. GKE Ingress, Cloud Run) the direct peer
    (request.client.host) is the LB's IP, so we honour X-Forwarded-For, whose
    LEFT-most value is the original client. Otherwise fall back to the peer IP.
    在代理/负载均衡器(如 GKE Ingress、Cloud Run)之后,直接对端(request.client.host)
    是 LB 的 IP,因此我们优先取 X-Forwarded-For 最左侧的值(即原始客户端).否则回退到对端 IP.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""


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
