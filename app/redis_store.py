"""Shared Redis state for operational user activity."""

from __future__ import annotations

import os


class RedisStore:
    """Async Redis adapter for the rolling distinct active-user count."""

    _KEY = "smartpark:active-users"
    _COUNT_SCRIPT = """
local now = redis.call('TIME')
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local cutoff = now_ms - tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. cutoff)
return redis.call('ZCARD', KEYS[1])
"""

    def __init__(self) -> None:
        self._client = None
        self._url = os.getenv("REDIS_URL", "redis://redis-service:6379/0")
        self._timeout_s = float(os.getenv("REDIS_TIMEOUT", "2"))

    @property
    def enabled(self) -> bool:
        return True

    def _get_client(self):
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=self._timeout_s,
                socket_timeout=self._timeout_s,
                health_check_interval=30,
            )
        return self._client

    async def check_connection(self, timeout_s: float = 5.0) -> bool:
        del timeout_s
        await self._get_client().ping()
        return True

    async def record_user(self, user_id: str) -> None:
        client = self._get_client()
        server_time = await client.time()
        timestamp_ms = int(server_time[0]) * 1000 + int(server_time[1]) // 1000
        await client.zadd(self._KEY, {user_id: timestamp_ms})

    async def count_recent_users(self, seconds: float = 30.0) -> int:
        if seconds <= 0:
            return 0
        result = await self._get_client().eval(
            self._COUNT_SCRIPT,
            1,
            self._KEY,
            int(seconds * 1000),
        )
        return int(result)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
