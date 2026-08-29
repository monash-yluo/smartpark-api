"""Small thread-safe in-memory TTL cache.
一个小型,线程安全的内存 TTL 缓存.

"Performance optimisation: repeated requests from the same user can be cached."
"性能优化:同一用户的重复请求可以被缓存."

The most natural cache key is (uuid, n): the same user asking for the same n car
parks within the TTL window gets the previous answer instantly, without re-hitting
all the cameras or re-running inference. This is a per-pod cache, which is fine for
the "same user repeats" optimisation; the handoff notes the multi-replica caveat
for the user-count OPS-API-2 (see logging_utils), which is a different concern.
最自然的缓存键是 (uuid, n):同一用户在 TTL 窗口内请求相同的 n 个车场,会立刻拿到上次的结果,
无需重新访问所有摄像头或重跑推理.这是一个按 Pod 的缓存,对"同一用户重复"的优化足够;
handoff 指出用户计数 OPS-API-2 有多副本的注意事项(见 logging_utils),那是另一个问题.
"""

from __future__ import annotations

import threading
import time


class TTLCache:
    def __init__(self, default_ttl: int = 30) -> None:
        self._ttl = default_ttl
        self._data: dict = {}  # key -> (expires_at, value)  键 -> (过期时间, 值)
        self._lock = threading.Lock()

    def get(self, key) -> object | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.time() > expires_at:
                del self._data[key]  # Expired; drop it. 已过期,删除.
                return None
            return value

    def set(self, key, value, ttl: int | None = None) -> None:
        with self._lock:
            self._data[key] = (time.time() + (ttl or self._ttl), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
