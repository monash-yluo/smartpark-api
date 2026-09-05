"""Resilience test for GET /api/find-carparks (CORE-API-1).

Verifies the change documented in doc/handoff-main-resilience.md:

  * Scenario G: uuid is required by CORE-API-1        -> missing/blank/too-long uuid -> HTTP 422 (FastAPI)
  * Scenario A: EVERY sampled car park camera fails  -> HTTP 503
  * Scenario D: a single inference raises (unit)      -> _analyze_carpark returns None
  * Scenario E: EVERY inference raises (e2e)          -> HTTP 503
  * Scenario C: EVERY car park succeeds               -> HTTP 200, failed_carparks == 0
  * Scenario B: SOME car parks fail                   -> HTTP 200, failed_carparks > 0 (no false 503)
    * OPS-API-1: all/some/no car parks available        -> complete status rows + summary

How to run (from the smartpark-api root, with the venv):
    .\\venv\\Scripts\\python.exe tests\\test_find_carparks_resilience.py

The script adds the project root to sys.path itself, so it also works if you
invoke it from another working directory with an absolute path.
"""

import asyncio
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- make the "app" package importable regardless of cwd --------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../smartpark-api
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- silence the (harmless) starlette/fastapi testclient deprecation warning
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings(
    "ignore", message=r"Using .* with .*starlette\.testclient"  # noqa: FS003
)

DEAD_URL = "http://127.0.0.1:1/"  # loopback port 1 => connection refused instantly

# A valid 1x1 PNG, base64-encoded -> a well-formed takephoto response.
_1x1_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360000002000200f97d3c4f0000000049454e44ae426082"
    )
).decode("ascii")


class _TakephotoHandler(BaseHTTPRequestHandler):
    """Tiny local stand-in for the takephoto (camera) service."""

    def do_GET(self):  # noqa: N802
        body = json.dumps(
            {"status": "success", "carpark_id": "mock", "image_base64": _1x1_PNG}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_mock_takephoto():
    """Start a background mock takephoto server; return (url, server)."""
    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _TakephotoHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/api/takephoto", srv


def _write_config(mode, mock_url=None, count=30):
    """Build a temp carparks.json.

    mode:
      "all_dead"   every car park points at DEAD_URL
      "all_ok"     every car park points at mock_url
      "mixed"      even car parks -> mock_url, odd car parks -> DEAD_URL
    Returns the path to the written JSON file.
    """
    carparks = []
    for i in range(1, count + 1):
        if mode == "all_dead":
            url = DEAD_URL
        elif mode == "all_ok":
            url = mock_url
        else:  # mixed
            url = mock_url if i % 2 == 0 else DEAD_URL
        carparks.append(
            {"id": f"CBD_{i:03d}", "name": f"Car Park CBD_{i:03d}", "takephoto_url": url}
        )
    path = Path(tempfile.mkdtemp()) / "carparks.json"
    path.write_text(json.dumps({"carparks": carparks}), encoding="utf-8")
    return path


def _patch_env(config_path):
    """Point the app at a given config and force the MockDetector."""
    os.environ["CARPARKS_CONFIG"] = str(config_path)
    # No MODEL_PATH -> build_detector() falls back to MockDetector (no YOLO needed).
    os.environ.pop("MODEL_PATH", None)


def run():
    import fastapi.testclient  # noqa: F401
    from app.main import (
        _analyze_carpark,
        _get_carpark_analysis,
        _shutdown_inflight_analyses,
        app,
    )
    from app.config import CarPark

    passed = 0
    total = 0

    def check(ok, label, detail=""):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
            print(f"  [PASS] {label}  {detail}")
        else:
            print(f"  [FAIL] {label}  {detail}")

    # -------------------------------------------------------------- G: uuid ----
    # CORE-API-1 requires uuid; FastAPI enforces it and returns 422 when missing /
    # blank / too long (validation handled by the framework, before any fetch).
    _patch_env(_write_config("all_dead"))
    with fastapi.testclient.TestClient(app) as client:
        for label, params in [
            ("missing", {"n": 3}),
            ("blank", {"uuid": "", "n": 3}),
            ("too_long", {"uuid": "x" * 65, "n": 3}),
        ]:
            r = client.get("/api/find-carparks", params=params)
            print(f"\n[G] uuid {label}")
            check(
                r.status_code == 422,
                "422 when uuid missing/invalid (FastAPI)",
                f"(status={r.status_code})",
            )
        # a valid uuid passes validation (with a dead config it then 503s).
        r2 = client.get("/api/find-carparks", params={"uuid": "ok-user", "n": 3})
        print("\n[G] uuid provided")
        check(
            r2.status_code == 503,
            "valid uuid accepted (not 4xx)",
            f"(status={r2.status_code})",
        )

    # ------------------------------------------------------------------ A ----
    # every sampled camera is down -> 503
    _patch_env(_write_config("all_dead"))
    with fastapi.testclient.TestClient(app) as client:
        r = client.get("/api/find-carparks", params={"uuid": "u-503", "n": 3})
        b = r.json()
        print("\n[A] all cameras down")
        check(
            r.status_code == 503
            and b["status"] == "error"
            and b["msg"] == "all sampled car parks are unavailable"
            and b["results"] == []
            and b["sampled_carparks"] == 6,
            "503 + empty results",
            f"(status={r.status_code}, sampled={b.get('sampled_carparks')})",
        )

        ops_response = client.get("/api/ops/carparks")
        ops_body = ops_response.json()
        print("\n[OPS] all car parks unavailable")
        check(
            ops_response.status_code == 503
            and ops_body["status"] == "error"
            and ops_body["total_carparks"] == 30
            and ops_body["available_carparks"] == 0
            and ops_body["unavailable_carparks"] == 30
            and len(ops_body["carparks"]) == 30
            and all(
                row["status"] == "unavailable"
                and row["available_spaces"] is None
                and row["confidence_score"] is None
                for row in ops_body["carparks"]
            ),
            "503 + every configured car park has an unavailable row",
            f"(status={ops_response.status_code}, rows={len(ops_body.get('carparks', []))})",
        )

    # ------------------------------------------------------------------ D ----
    # a single inference raising is swallowed -> _analyze_carpark returns None
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "success", "image_base64": _1x1_PNG}

    class _FakeHttp:
        async def get(self, url, params=None, timeout=None):
            return _FakeResp()

        async def aclose(self):
            pass

    class _RaiserDetector:
        async def analyze(self, image_bytes):
            raise RuntimeError("inference boom")

    app.state.http = _FakeHttp()
    app.state.detector = _RaiserDetector()
    cp = CarPark(id="X_001", name="X 001", takephoto_url="http://mock")
    result = asyncio.run(_analyze_carpark(cp))
    print("\n[D] single inference raises (unit)")
    check(result is None, "_analyze_carpark returns None", f"(got={result})")

    # --------------------------------------------------------- single-flight ----
    # Concurrent cache misses share one detector call per car park. The counting
    # detector stands in for YOLO here: its analyze() method is the exact call
    # made by the production YOLO wrapper.
    class _CountingHttp:
        def __init__(self):
            self.calls = 0

        async def get(self, url, params=None, timeout=None):
            self.calls += 1
            await asyncio.sleep(0.01)
            return _FakeResp()

    class _CountingDetector:
        def __init__(self):
            self.calls = 0

        async def analyze(self, image_bytes):
            self.calls += 1
            await asyncio.sleep(0.01)
            return {
                "available_spaces": 7,
                "occupied_spaces": 3,
                "confidence_score": 0.9,
                "annotated_png": image_bytes,
            }

    counting_http = _CountingHttp()
    counting_detector = _CountingDetector()
    app.state.cache.clear()
    app.state.http = counting_http
    app.state.detector = counting_detector

    second_cp = CarPark(id="X_002", name="X 002", takephoto_url="http://mock")

    async def _single_flight_analysis():
        concurrent_analyses = await asyncio.gather(
            _get_carpark_analysis(cp),
            _get_carpark_analysis(cp),
            _get_carpark_analysis(second_cp),
            _get_carpark_analysis(second_cp),
        )
        calls_after_concurrency = (
            counting_http.calls,
            counting_detector.calls,
        )
        cached_analyses = await asyncio.gather(
            _get_carpark_analysis(cp), _get_carpark_analysis(second_cp)
        )
        return concurrent_analyses, cached_analyses, calls_after_concurrency

    analyses, cached_analyses, calls_after_concurrency = asyncio.run(
        _single_flight_analysis()
    )
    print("\n[single-flight] concurrent analysis and cache reuse")
    check(
        calls_after_concurrency == (2, 2)
        and counting_http.calls == 2
        and counting_detector.calls == 2
        and analyses[0] == analyses[1]
        and analyses[2] == analyses[3]
        and cached_analyses[0] == analyses[0]
        and cached_analyses[1] == analyses[2]
        and not app.state.inflight_analyses,
        "one fetch and YOLO-style inference task per car park; later calls use cache",
        f"(fetches={counting_http.calls}, inferences={counting_detector.calls})",
    )

    # A valid cache entry in the refresh window returns immediately while a
    # single background task refreshes it for subsequent requests.
    refresh_started = asyncio.Event()
    refresh_release = asyncio.Event()
    old_analysis = {
        "available_spaces": 4,
        "occupied_spaces": 6,
        "confidence_score": 0.8,
        "annotated_png": b"old",
    }
    refreshed_analysis = {
        "available_spaces": 9,
        "occupied_spaces": 1,
        "confidence_score": 0.95,
        "annotated_png": b"new",
    }

    class _RefreshDetector:
        def __init__(self):
            self.calls = 0

        async def analyze(self, image_bytes):
            self.calls += 1
            refresh_started.set()
            await refresh_release.wait()
            return refreshed_analysis

    refresh_detector = _RefreshDetector()
    refresh_cache_key = ("carpark-analysis", cp.id)
    app.state.cache.clear()
    app.state.cache.set(refresh_cache_key, old_analysis)
    created_at, expires_at, value = app.state.cache._data[refresh_cache_key]
    app.state.cache._data[refresh_cache_key] = (created_at - 21, expires_at, value)
    app.state.http = _CountingHttp()
    app.state.detector = refresh_detector

    async def _refresh_ahead_analysis():
        immediate_results = await asyncio.gather(
            _get_carpark_analysis(cp), _get_carpark_analysis(cp)
        )
        await refresh_started.wait()
        refresh_task = app.state.inflight_analyses[cp.id]
        refresh_release.set()
        await asyncio.shield(refresh_task)
        return immediate_results, await _get_carpark_analysis(cp)

    immediate_results, refreshed_result = asyncio.run(_refresh_ahead_analysis())
    print("\n[refresh-ahead] valid cache returns while one task refreshes it")
    check(
        immediate_results == [old_analysis, old_analysis]
        and refresh_detector.calls == 1
        and refreshed_result == refreshed_analysis
        and app.state.cache.get(refresh_cache_key) == refreshed_analysis
        and not app.state.inflight_analyses,
        "cached result returns immediately; one background task updates the cache",
        f"(inferences={refresh_detector.calls})",
    )

    # A cancelled waiter must not cancel the shared fetch/inference task. The
    # remaining waiter should receive the result, and the successful result
    # should still be written to the cache.
    cancellation_started = asyncio.Event()
    cancellation_release = asyncio.Event()

    class _BlockingDetector:
        def __init__(self):
            self.calls = 0

        async def analyze(self, image_bytes):
            self.calls += 1
            cancellation_started.set()
            await cancellation_release.wait()
            return {
                "available_spaces": 11,
                "occupied_spaces": 1,
                "confidence_score": 0.95,
                "annotated_png": image_bytes,
            }

    blocking_detector = _BlockingDetector()
    app.state.cache.clear()
    app.state.http = _CountingHttp()
    app.state.detector = blocking_detector

    async def _cancelled_waiter_analysis():
        cancelled_waiter = asyncio.create_task(_get_carpark_analysis(cp))
        await cancellation_started.wait()
        remaining_waiter = asyncio.create_task(_get_carpark_analysis(cp))
        cancelled_waiter.cancel()
        try:
            await cancelled_waiter
        except asyncio.CancelledError:
            pass
        cancellation_release.set()
        remaining_result = await remaining_waiter
        cached_result = await _get_carpark_analysis(cp)
        return remaining_result, cached_result

    remaining_result, cached_result = asyncio.run(_cancelled_waiter_analysis())
    print("\n[cancellation] cancelled waiter does not cancel shared analysis")
    check(
        blocking_detector.calls == 1
        and remaining_result is not None
        and cached_result == remaining_result
        and app.state.cache.get(("carpark-analysis", cp.id)) == remaining_result
        and not app.state.inflight_analyses,
        "cancelled waiter leaves shared task alive; result reaches waiter and cache",
        f"(inferences={blocking_detector.calls})",
    )

    # Shutdown cancels in-flight work and waits for it before the HTTP client
    # is closed, so the task cannot continue using a shutdown resource.
    shutdown_events = []

    async def _shutdown_probe():
        analysis_started = asyncio.Event()

        async def pending_analysis():
            try:
                analysis_started.set()
                await asyncio.Event().wait()
            finally:
                shutdown_events.append("task-finished")

        class _ShutdownHttp:
            async def aclose(self):
                shutdown_events.append("http-closed")
                assert "task-finished" in shutdown_events

        pending_task = asyncio.create_task(pending_analysis())
        app.state.inflight_analyses = {cp.id: pending_task}
        app.state.http = _ShutdownHttp()
        await analysis_started.wait()
        await _shutdown_inflight_analyses(app)
        await app.state.http.aclose()
        return pending_task

    shutdown_task = asyncio.run(_shutdown_probe())
    print("\n[shutdown] in-flight analysis tasks are cancelled before HTTP close")
    check(
        shutdown_task.cancelled()
        and shutdown_events == ["task-finished", "http-closed"]
        and not app.state.inflight_analyses,
        "shutdown awaits cancelled analysis before closing HTTP client",
        f"(events={shutdown_events})",
    )

    # ------------------------------------------------------------------ E ----
    # every inference raises (fetch succeeds) -> 503
    _patch_env(_write_config("all_ok", "http://mock"))
    with fastapi.testclient.TestClient(app) as client:
        app.state.http = _FakeHttp()
        app.state.detector = _RaiserDetector()
        r = client.get("/api/find-carparks", params={"uuid": "u-503-inf", "n": 3})
        b = r.json()
        print("\n[E] all inferences raise (e2e)")
        check(
            r.status_code == 503 and b["msg"] == "all sampled car parks are unavailable",
            "503 returned when all inferences fail",
            f"(status={r.status_code})",
        )

    # -------------------------------------------------------------- B / C ----
    mock_url, srv = _start_mock_takephoto()
    try:
        # C: every car park succeeds -> 200, failed_carparks == 0
        _patch_env(_write_config("all_ok", mock_url))
        with fastapi.testclient.TestClient(app) as client:
            r = client.get("/api/find-carparks", params={"uuid": "u-ok", "n": 30})
            b = r.json()
            print("\n[C] all car parks succeed")
            check(
                r.status_code == 200 and b["failed_carparks"] == 0 and len(b["results"]) == 30,
                "200, no false 503",
                f"(status={r.status_code}, failed={b.get('failed_carparks')}, results={len(b.get('results', []))})",
            )

            ops_response = client.get("/api/ops/carparks")
            ops_body = ops_response.json()
            print("\n[OPS] all car parks available")
            check(
                ops_response.status_code == 200
                and ops_body["status"] == "success"
                and ops_body["total_carparks"] == 30
                and ops_body["available_carparks"] == 30
                and ops_body["unavailable_carparks"] == 0
                and len(ops_body["carparks"]) == 30
                and all(row["status"] == "available" for row in ops_body["carparks"]),
                "200 + every configured car park has an available row",
                f"(status={ops_response.status_code}, rows={len(ops_body.get('carparks', []))})",
            )

        # B: half fail -> 200, failed_carparks > 0
        _patch_env(_write_config("mixed", mock_url))
        with fastapi.testclient.TestClient(app) as client2:
            r2 = client2.get("/api/find-carparks", params={"uuid": "u-mixed", "n": 30})
            b2 = r2.json()
            print("\n[B] some car parks fail")
            check(
                r2.status_code == 200 and b2["failed_carparks"] > 0,
                "200 + failed_carparks disclosed",
                f"(status={r2.status_code}, failed={b2.get('failed_carparks')}, results={len(b2.get('results', []))})",
            )

            ops_response = client2.get("/api/ops/carparks")
            ops_body = ops_response.json()
            statuses = {row["status"] for row in ops_body["carparks"]}
            print("\n[OPS] some car parks unavailable")
            check(
                ops_response.status_code == 200
                and ops_body["status"] == "partial"
                and ops_body["total_carparks"] == 30
                and ops_body["available_carparks"] == 15
                and ops_body["unavailable_carparks"] == 15
                and len(ops_body["carparks"]) == 30
                and statuses == {"available", "unavailable"},
                "200 partial + every configured car park has a status row",
                f"(available={ops_body.get('available_carparks')}, unavailable={ops_body.get('unavailable_carparks')})",
            )
    finally:
        srv.shutdown()
        srv.server_close()

    print(f"\n===== RESULT: {passed}/{total} passed =====")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    run()
