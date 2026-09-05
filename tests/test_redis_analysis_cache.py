import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cache import CacheLookup, TTLCache
from app.config import CarPark
from app.firestore_store import FirestoreStore
from app.main import _get_carpark_analysis, app, ops_carparks
from app.redis_store import RedisStore


class _FakeRedisClient:
    def __init__(self):
        self.values = {}
        self.last_expiry = None

    async def set(self, key, value, ex):
        self.values[key] = value
        self.last_expiry = ex

    async def get(self, key):
        return self.values.get(key)


class RedisAnalysisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_round_trip_uses_requested_ttl(self):
        store = RedisStore()
        client = _FakeRedisClient()
        store._client = client
        analysis = {
            "available_spaces": 12,
            "occupied_spaces": 5,
            "confidence_score": 0.91,
            "annotated_png": b"\x89PNG\r\n",
        }

        await store.set_analysis("CBD_001", analysis, 30)
        cached = await store.get_analysis("CBD_001", 20)

        self.assertEqual(client.last_expiry, 30)
        self.assertIsNotNone(cached)
        self.assertEqual(
            {key: value for key, value in cached.value.items() if key != "_created_at"},
            analysis,
        )
        self.assertIsInstance(cached.value["_created_at"], float)
        self.assertFalse(cached.should_refresh)

    async def test_l2_hit_is_returned_without_l1_backfill(self):
        carpark = CarPark("CBD_001", "One", "http://camera")
        shared_analysis = {
            "available_spaces": 9,
            "occupied_spaces": 3,
            "confidence_score": 0.88,
            "annotated_png": b"shared",
        }
        store = RedisStore()
        store.get_analysis = AsyncMock(
            return_value=CacheLookup(shared_analysis, should_refresh=False)
        )
        app.state.user_activity = store
        app.state.cache = TTLCache(default_ttl=30)
        app.state.config = SimpleNamespace(request_cache_refresh_after_s=20)
        app.state.inflight_analyses = {}
        app.state.inflight_analyses_lock = asyncio.Lock()

        result = await _get_carpark_analysis(carpark)

        self.assertEqual(result, shared_analysis)
        self.assertIsNone(app.state.cache.get(("carpark-analysis", carpark.id)))
        store.get_analysis.assert_awaited_once_with(carpark.id, 20)

    async def test_ops_carparks_uses_l2_created_at_without_l1_backfill(self):
        carpark = CarPark("CBD_004", "Four", "http://camera")
        created_at = 1_725_600_000.0
        shared_analysis = {
            "available_spaces": 8,
            "occupied_spaces": 6,
            "confidence_score": 0.87,
            "annotated_png": b"shared",
            "_created_at": created_at,
        }
        store = RedisStore()
        store.get_analysis = AsyncMock(
            return_value=CacheLookup(shared_analysis, should_refresh=False)
        )
        app.state.user_activity = store
        app.state.cache = TTLCache(default_ttl=30)
        app.state.config = SimpleNamespace(
            carparks=(carpark,),
            request_cache_refresh_after_s=20,
        )
        app.state.inflight_analyses = {}
        app.state.inflight_analyses_lock = asyncio.Lock()

        response = await ops_carparks()

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["available_carparks"], 1)
        self.assertEqual(response["unavailable_carparks"], 0)
        self.assertEqual(response["carparks"][0]["carpark_id"], carpark.id)
        self.assertEqual(
            response["carparks"][0]["created_at"],
            datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
        )
        self.assertNotIn("_created_at", response["carparks"][0])
        self.assertIsNone(app.state.cache.get(("carpark-analysis", carpark.id)))
        store.get_analysis.assert_awaited_once_with(carpark.id, 20)

    async def test_l2_miss_runs_inference_and_writes_both_caches(self):
        carpark = CarPark("CBD_003", "Three", "http://camera")
        analysis = {
            "available_spaces": 11,
            "occupied_spaces": 2,
            "confidence_score": 0.95,
            "annotated_png": b"fresh",
        }
        store = RedisStore()
        store.get_analysis = AsyncMock(return_value=None)
        store.set_analysis = AsyncMock()
        app.state.user_activity = store
        app.state.cache = TTLCache(default_ttl=30)
        app.state.config = SimpleNamespace(
            request_cache_refresh_after_s=20,
            request_cache_ttl_s=30,
            takephoto_timeout_s=1,
        )
        app.state.inflight_analyses = {}
        app.state.inflight_analyses_lock = asyncio.Lock()
        app.state.http = object()
        app.state.detector = SimpleNamespace(analyze=AsyncMock(return_value=analysis))

        with patch("app.main.fetch_image", AsyncMock(return_value=b"photo")):
            result = await _get_carpark_analysis(carpark)

        self.assertEqual(result, analysis)
        self.assertEqual(
            app.state.cache.get(("carpark-analysis", carpark.id)), analysis
        )
        store.get_analysis.assert_awaited_once_with(carpark.id, 20)
        store.set_analysis.assert_awaited_once_with(carpark.id, analysis, 30)

    async def test_firestore_mode_uses_only_l1_and_inference(self):
        carpark = CarPark("CBD_002", "Two", "http://camera")
        analysis = {
            "available_spaces": 7,
            "occupied_spaces": 4,
            "confidence_score": 0.82,
            "annotated_png": b"local",
        }
        previous_enabled = os.environ.get("FIRESTORE_ENABLED")
        os.environ["FIRESTORE_ENABLED"] = "0"
        try:
            app.state.user_activity = FirestoreStore()
        finally:
            if previous_enabled is None:
                os.environ.pop("FIRESTORE_ENABLED", None)
            else:
                os.environ["FIRESTORE_ENABLED"] = previous_enabled
        app.state.cache = TTLCache(default_ttl=30)
        app.state.config = SimpleNamespace(
            request_cache_refresh_after_s=20,
            request_cache_ttl_s=30,
            takephoto_timeout_s=1,
        )
        app.state.inflight_analyses = {}
        app.state.inflight_analyses_lock = asyncio.Lock()
        app.state.http = object()
        app.state.detector = SimpleNamespace(analyze=AsyncMock(return_value=analysis))

        with patch("app.main.fetch_image", AsyncMock(return_value=b"photo")):
            result = await _get_carpark_analysis(carpark)

        self.assertEqual(result, analysis)
        self.assertEqual(
            app.state.cache.get(("carpark-analysis", carpark.id)), analysis
        )


if __name__ == "__main__":
    unittest.main()