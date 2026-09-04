"""Shared Firestore state for operational user activity."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone


class FirestoreStore:
    """Small async adapter for the shared active-user collection."""

    def __init__(self) -> None:
        self._client = None
        self._disabled = os.getenv("FIRESTORE_ENABLED", "1").lower() not in {
            "1",
            "true",
            "yes",
        }

    @property
    def enabled(self) -> bool:
        return not self._disabled

    def _get_client(self):
        if self._disabled:
            return None
        if self._client is None:
            from google.cloud.firestore_v1 import AsyncClient, SERVER_TIMESTAMP

            self._client = AsyncClient()
            self._server_timestamp = SERVER_TIMESTAMP
        return self._client

    @staticmethod
    def _document_id(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    async def record_user(self, user_id: str) -> None:
        client = self._get_client()
        if client is None:
            return
        await client.collection("active_users").document(self._document_id(user_id)).set(
            {"last_seen_at": self._server_timestamp}, merge=True
        )

    async def count_recent_users(self, seconds: float = 30.0) -> int:
        client = self._get_client()
        if client is None:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        query = client.collection("active_users").where(
            "last_seen_at", ">=", cutoff
        )
        return sum(1 async for _ in query.stream())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
