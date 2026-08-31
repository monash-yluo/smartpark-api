"""Small thread-safe in-memory TTL cache.
一个小型,线程安全的内存 TTL 缓存.

"Performance optimisation: complete analyses are cached by car park ID."
"性能优化:完整推理结果按停车场 ID 缓存."

The application stores the successful inference output (counts, confidence, and
annotated PNG) under a namespaced car-park key. This per-pod cache allows the list,
annotation, and operations endpoints to share one fetched image and one model run
within the TTL window.
应用将成功的推理输出(计数,置信度和标注 PNG)存储在带命名空间的停车场键下.这个按 Pod
的缓存使列表,标注和运营端点能在 TTL 内共享一次拉图和模型推理.
"""

from __future__ import annotations

import threading
import time


class TTLCache:
    """线程安全的内存 TTL 缓存.

    数据结构:self._data 是 键 -> (过期时间戳, 值) 的字典.
    写入时根据 ttl 计算过期时间,读取时惰性删除已过期条目.
    """

    def __init__(self, default_ttl: int = 30) -> None:
        self._ttl = default_ttl  # 默认过期秒数(未单独指定 ttl 时使用)
        self._data: dict = {}  # key -> (expires_at, value)  键 -> (过期时间, 值)
        self._lock = threading.Lock()  # 互斥锁,保证多线程(多请求)读写安全

    def get(self, key) -> object | None:
        """读取缓存;键不存在或已过期时返回 None."""
        with self._lock:  # 加锁,防止并发读写竞争
            item = self._data.get(key)
            if item is None:
                return None  # 键不存在
            expires_at, value = item
            if time.time() > expires_at:  # 当前时间超过过期时间 => 已过期
                del self._data[key]  # Expired; drop it. 已过期,删除(惰性清理).
                return None
            return value  # 未过期,直接返回缓存值

    def set(self, key, value, ttl: int | None = None) -> None:
        """写入缓存;ttl 为 None 时使用默认 self._ttl."""
        with self._lock:
            # 过期时间 = 当前时间 + ttl(或默认值);(ttl or self._ttl) 兼容 ttl 为 None/0 的情况
            self._data[key] = (time.time() + (ttl or self._ttl), value)

    def clear(self) -> None:
        """清空所有缓存条目."""
        with self._lock:
            self._data.clear()
