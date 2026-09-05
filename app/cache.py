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

from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class CacheLookup:
    """缓存查询结果及其是否需要后台刷新的标记。"""

    value: object
    should_refresh: bool


class TTLCache:
    """线程安全的内存 TTL 缓存.

    数据结构:self._data 是 键 -> (过期时间戳, 值) 的字典.
    写入时根据 ttl 计算过期时间,读取时惰性删除已过期条目.
    """

    def __init__(self, default_ttl: int = 30) -> None:
        self._ttl = default_ttl  # 默认过期秒数(未单独指定 ttl 时使用)
        self._data: dict = {}  # key -> (created_at, expires_at, value)
        self._lock = threading.Lock()  # 互斥锁,保证多线程(多请求)读写安全

    def get(self, key) -> object | None:
        """读取缓存;键不存在或已过期时返回 None."""
        with self._lock:  # 加锁,防止并发读写竞争
            item = self._data.get(key)
            if item is None:
                return None  # 键不存在
            _, expires_at, value = item
            if time.time() > expires_at:  # 当前时间超过过期时间 => 已过期
                del self._data[key]  # Expired; drop it. 已过期,删除(惰性清理).
                return None
            return value  # 未过期,直接返回缓存值

    def get_with_refresh(
        self, key, refresh_after: float
    ) -> CacheLookup | None:
        """读取未过期缓存，并标记是否应在后台提前刷新。

        当缓存年龄达到 ``refresh_after`` 但尚未达到 TTL 时，仍返回当前值，
        由调用方异步启动刷新任务。超过 TTL 后删除条目并返回 ``None``,
        因此该方法不会返回超过最大年龄的数据。
        """
        # 刷新阈值按秒计算，负数没有合理语义，尽早拒绝错误配置。
        if refresh_after < 0:
            raise ValueError("refresh_after must be non-negative")

        # 读取缓存记录与计算其年龄必须在同一把锁内，避免并发 set()/clear()
        # 造成读取到不一致的记录。
        with self._lock:
            item = self._data.get(key)
            if item is None:
                # 从未写入该键，调用方需要同步获取并生成第一份缓存结果。
                return None

            created_at, expires_at, value = item
            now = time.time()
            if now > expires_at:
                # 严格遵守 TTL：已过期的数据绝不返回，删除后由调用方等待刷新结果。
                del self._data[key]
                return None

            # 缓存仍有效时立即返回；达到刷新阈值仅标记 should_refresh，
            # 由调用方在后台复用共享 Task 更新缓存，不阻塞当前请求。
            age = now - created_at
            return CacheLookup(
                value=value,
                should_refresh=age >= refresh_after,
            )

    def set(self, key, value, ttl: int | None = None) -> None:
        """写入缓存;ttl 为 None 时使用默认 self._ttl."""
        with self._lock:
            # 过期时间 = 当前时间 + ttl(或默认值);(ttl or self._ttl) 兼容 ttl 为 None/0 的情况
            created_at = time.time()
            self._data[key] = (
                created_at,
                created_at + (ttl or self._ttl),
                value,
            )

    def get_created_at(self, key) -> float | None:
        """返回未过期缓存条目的创建时间戳;不存在或过期时返回 None."""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            created_at, expires_at, _ = item
            if time.time() > expires_at:
                del self._data[key]
                return None
            return created_at

    def clear(self) -> None:
        """清空所有缓存条目."""
        with self._lock:
            self._data.clear()
