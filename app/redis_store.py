"""用于运营用户统计和跨 Pod 分析缓存的 Redis 共享状态。 / Redis-backed shared state for operational user counting and cross-Pod analysis caching."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid

from .cache import CacheLookup


class RedisStore:
    """封装活跃用户统计、L2 分析缓存和刷新锁的异步 Redis 适配器。 / Async Redis adapter for active-user counting, L2 analysis caching, and refresh locks."""

    # ZSET 的 member 是稳定用户 ID，score 是该用户最后访问时的 Redis 服务端毫秒时间戳。 / ZSET members are stable user IDs; scores are Redis-server timestamps in milliseconds.
    _KEY = "smartpark:active-users"
    # 每个停车场使用独立的分析缓存键和刷新锁键。 / Each car park has a separate analysis-cache key and refresh-lock key.
    _ANALYSIS_KEY_PREFIX = "smartpark:analysis:"
    _REFRESH_LOCK_KEY_PREFIX = "smartpark:analysis-refresh-lock:"

    # 仅当锁中的 token 仍属于当前持有者时才删除，避免误删锁过期后由其他 Pod 获得的新锁。 / Delete a lock only when its token still belongs to this owner, preventing removal of a newer lock acquired by another Pod after expiry.
    _RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
    # 在 Redis 内原子地读取服务端时间、移除窗口外用户并计数，避免多个 Pod 各自时钟偏差或清理与计数之间的竞态。 / Atomically read Redis time, remove users outside the window, and count the remainder, avoiding Pod clock skew and cleanup/count races.
    _COUNT_SCRIPT = """
local now = redis.call('TIME')
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local cutoff = now_ms - tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. cutoff)
return redis.call('ZCARD', KEYS[1])
"""

    def __init__(self) -> None:
        # 延迟创建客户端，使应用导入和不使用 Redis 的 Firestore 模式不需要立即连接 Redis。 / Create the client lazily so imports and Firestore mode do not connect to Redis immediately.
        self._client = None
        self._url = os.getenv("REDIS_URL", "redis://redis-service:6379/0")
        self._timeout_s = float(os.getenv("REDIS_TIMEOUT", "2"))

    @property
    def enabled(self) -> bool:
        # 构造 RedisStore 即表示 Redis 后端已被选中；连接是否可达由 check_connection 单独检查。 / Constructing RedisStore means Redis was selected; check_connection separately verifies reachability.
        return True

    def _get_client(self):
        """按需创建并复用异步 Redis 客户端。 / Create and reuse the asynchronous Redis client on demand."""
        if self._client is None:
            from redis.asyncio import Redis

            # decode_responses=True 让普通 Redis 字符串返回 str；缓存中的 PNG 会先经过 Base64，因此仍可安全存储。 / decode_responses=True returns normal Redis strings as str; cached PNG bytes remain safe because they are Base64-encoded first.
            self._client = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=self._timeout_s,
                socket_timeout=self._timeout_s,
                health_check_interval=30,
            )
        return self._client

    async def check_connection(self, timeout_s: float = 5.0) -> bool:
        """发送 PING 验证 Redis 是否可访问。 / Send PING to verify that Redis is reachable."""
        # 实际超时由创建客户端时的 REDIS_TIMEOUT 控制；保留参数以匹配共享 store 接口。 / The client-level REDIS_TIMEOUT controls the request; keep this parameter to match the shared-store interface.
        del timeout_s
        await self._get_client().ping()
        return True

    async def record_user(self, user_id: str) -> None:
        """写入或更新用户最后活动时间。 / Insert or update a user's last-active timestamp."""
        client = self._get_client()
        # 使用 Redis TIME 让所有 API Pod 采用同一个时钟来源。 / Use Redis TIME so every API Pod shares the same clock source.
        server_time = await client.time()
        timestamp_ms = int(server_time[0]) * 1000 + int(server_time[1]) // 1000
        # ZADD 会覆盖同一 member 的旧 score，因此同一用户在统计窗口内始终只占一个成员。 / ZADD replaces the old score for the same member, so one user occupies only one entry in the window.
        await client.zadd(self._KEY, {user_id: timestamp_ms})

    async def count_recent_users(self, seconds: float = 30.0) -> int:
        """返回滚动时间窗口内不同活跃用户的数量。 / Return the distinct active-user count within a rolling time window."""
        if seconds <= 0:
            return 0
        # Lua 脚本把清理过期 member 和 ZCARD 合并成一个原子操作。 / The Lua script combines expired-member cleanup and ZCARD into one atomic operation.
        result = await self._get_client().eval(
            self._COUNT_SCRIPT,
            1,
            self._KEY,
            int(seconds * 1000),
        )
        return int(result)

    async def set_analysis(
        self, carpark_id: str, analysis: dict, ttl_s: int
    ) -> None:
        """将一次成功的停车场分析写入共享 L2 缓存。 / Store a successful car-park analysis in the shared L2 cache."""
        # JSON 负责结构化字段，PNG 字节先编码为 Base64；created_at 用于判断何时进入后台刷新窗口。 / JSON carries structured fields, PNG bytes are Base64-encoded, and created_at determines when background refresh should begin.
        payload = json.dumps(
            {
                "created_at": time.time(),
                "available_spaces": analysis["available_spaces"],
                "occupied_spaces": analysis["occupied_spaces"],
                "confidence_score": analysis["confidence_score"],
                "annotated_png": base64.b64encode(
                    analysis["annotated_png"]
                ).decode("ascii"),
            },
            separators=(",", ":"),
        )
        # Redis EX 实施严格 TTL；键到期后下一次读取会重新执行图片获取和推理。 / Redis EX enforces the hard TTL; after expiry, the next read fetches an image and runs inference again.
        await self._get_client().set(
            f"{self._ANALYSIS_KEY_PREFIX}{carpark_id}",
            payload,
            ex=ttl_s,
        )

    async def get_analysis(
        self, carpark_id: str, refresh_after_s: float
    ) -> CacheLookup | None:
        """读取并还原 L2 分析，附带是否应后台刷新的判断。 / Read and reconstruct an L2 analysis together with its background-refresh decision."""
        payload = await self._get_client().get(
            f"{self._ANALYSIS_KEY_PREFIX}{carpark_id}"
        )
        if payload is None:
            return None

        # 恢复明确的数据类型，并保留内部 _created_at 供 OPS API 报告数据生成时间。 / Restore explicit data types and retain internal _created_at so the OPS API can report when the data was produced.
        decoded = json.loads(payload)
        created_at = float(decoded["created_at"])
        analysis = {
            "available_spaces": int(decoded["available_spaces"]),
            "occupied_spaces": int(decoded["occupied_spaces"]),
            "confidence_score": float(decoded["confidence_score"]),
            "annotated_png": base64.b64decode(decoded["annotated_png"], validate=True),
            "_created_at": created_at,
        }
        # refresh_after 只是软刷新阈值；超过它仍返回当前值，调用方会尝试在后台刷新，严格过期仍由 Redis TTL 决定。 / refresh_after is a soft threshold: the current value is still returned while the caller attempts a background refresh; Redis TTL controls hard expiry.
        return CacheLookup(
            value=analysis,
            should_refresh=time.time() - created_at >= refresh_after_s,
        )

    async def acquire_refresh_lock(
        self, carpark_id: str, ttl_s: int
    ) -> str | None:
        """尝试取得停车场刷新锁，成功时返回所有权 token。 / Try to acquire a car-park refresh lock and return its ownership token on success."""
        token = uuid.uuid4().hex
        # NX 保证同一时刻只有一个 Pod 获锁；EX 防止持有者崩溃后留下永久死锁。 / NX allows only one Pod to acquire the lock; EX prevents a crashed owner from leaving a permanent deadlock.
        acquired = await self._get_client().set(
            f"{self._REFRESH_LOCK_KEY_PREFIX}{carpark_id}",
            token,
            nx=True,
            ex=ttl_s,
        )
        return token if acquired else None

    async def release_refresh_lock(self, carpark_id: str, token: str) -> bool:
        """仅在 token 匹配时释放刷新锁。 / Release the refresh lock only when the token matches."""
        # 比较和删除必须在一个 Lua 脚本内原子执行，不能拆成 GET 后 DEL。 / Comparison and deletion must be atomic in one Lua script, not separate GET and DEL calls.
        released = await self._get_client().eval(
            self._RELEASE_LOCK_SCRIPT,
            1,
            f"{self._REFRESH_LOCK_KEY_PREFIX}{carpark_id}",
            token,
        )
        return bool(released)

    async def close(self) -> None:
        """关闭已创建的连接池；从未使用 Redis 时无需清理。 / Close the created connection pool; no cleanup is needed if Redis was never used."""
        if self._client is not None:
            await self._client.aclose()
