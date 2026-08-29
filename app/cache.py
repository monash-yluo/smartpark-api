"""Small thread-safe in-memory TTL cache.

"Performance optimisation: repeated requests from the same user can be cached."

The most natural cache key is (uuid, n): the same user asking for the same n car
parks within the TTL window gets the previous answer instantly, without re-hitting
all the cameras or re-running inference. This is a per-pod cache, which is fine for
the "same user repeats" optimisation; the handoff notes the multi-replica caveat
for the *user-count* OPS-API-2 (see logging_utils), which is a different concern.
"""

from __future__ import annotations

import threading
import time


class TTLCache:
    def __init__(self, default_ttl: int = 30) -> None:
        self._ttl = default_ttl
        self._data: dict = {}  # key -> (expires_at, value)
        self._lock = threading.Lock()

    def get(self, key) -> object | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.time() > expires_at:
                del self._data[key]
                return None
            return value

    def set(self, key, value, ttl: int | None = None) -> None:
        with self._lock:
            self._data[key] = (time.time() + (ttl or self._ttl), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
