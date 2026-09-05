"""Select the shared operational user-activity backend at startup."""

from __future__ import annotations

import os

from .firestore_store import FirestoreStore
from .redis_store import RedisStore


def build_user_activity_store():
    backend = os.getenv("USER_ACTIVITY_STORE", "firestore").strip().lower()
    if backend == "redis":
        return RedisStore()
    if backend == "firestore":
        return FirestoreStore()
    raise ValueError(
        "USER_ACTIVITY_STORE must be either 'redis' or 'firestore'"
    )
