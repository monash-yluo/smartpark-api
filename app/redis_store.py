"""Shared Redis state for operational user activity."""

from __future__ import annotations

import base64
import json
import os
import time

from .cache import CacheLookup


class RedisStore:
    """Async Redis adapter for the rolling distinct active-user count."""

    _KEY = "smartpark:active-users"
    _ANALYSIS_KEY_PREFIX = "smartpark:analysis:"
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

    async def set_analysis(
        self, carpark_id: str, analysis: dict, ttl_s: int
    ) -> None:
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
        await self._get_client().set(
            f"{self._ANALYSIS_KEY_PREFIX}{carpark_id}",
            payload,
            ex=ttl_s,
        )

    async def get_analysis(
        self, carpark_id: str, refresh_after_s: float
    ) -> CacheLookup | None:
        payload = await self._get_client().get(
            f"{self._ANALYSIS_KEY_PREFIX}{carpark_id}"
        )
        if payload is None:
            return None

        decoded = json.loads(payload)
        created_at = float(decoded["created_at"])
        analysis = {
            "available_spaces": int(decoded["available_spaces"]),
            "occupied_spaces": int(decoded["occupied_spaces"]),
            "confidence_score": float(decoded["confidence_score"]),
            "annotated_png": base64.b64decode(decoded["annotated_png"], validate=True),
            "_created_at": created_at,
        }
        return CacheLookup(
            value=analysis,
            should_refresh=time.time() - created_at >= refresh_after_s,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
