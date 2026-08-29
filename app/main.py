"""SmartPark main platform API.

A single FastAPI service that is horizontally scaled on GKE (1/2/4/8 replicas)
behind a LoadBalancer and HPA. This is the graded object — it is what Locust
hammers. It is a MONOLITH (not microservices): one service, many replicas.

Endpoints (per assignment):
  CORE-API-1  GET /api/find-carparks?uuid=...&n=3     top n car parks by free spaces
  CORE-API-2  GET /api/annotate-carpark?carpark_id=   annotated image, base64
  OPS-API-1   GET /api/ops/carparks                  list all car parks + free spaces
  OPS-API-2   GET /api/ops/users                     distinct users in last 30s

OPS-REQ-2 (operational dashboard) is intentionally deferred to a later step; the
module header in app/dashboard.py (not yet created) will hold it. The logging
requirement (OPS-REQ-1) is handled by logging_utils + the middleware below.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .cache import TTLCache
from .config import CarPark, load_platform_config
from .inference import build_detector
from .logging_utils import (
    count_unique_users,
    current_uuid,
    log,
    log_request,
    resolve_uuid,
)
from .takephoto import TakephotoError, fetch_image

log = logging.getLogger("smartpark.api")


# ---------------------------------------------------------------------------
# Lifespan: load config, model, cache, HTTP client once at startup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_platform_config()
    app.state.config = config
    app.state.cache = TTLCache(default_ttl=config.request_cache_ttl_s)
    # The model is loaded from disk at runtime (MODEL_PATH). If it is not
    # available (e.g. local dev), build_detector returns a MockDetector so the
    # endpoints still boot and can be tested. On GKE, MODEL_PATH is a mounted PVC.
    app.state.detector = build_detector(config.model_path, config.inference_workers)
    app.state.http = httpx.AsyncClient(timeout=config.takephoto_timeout_s)

    log.info(
        "started | carparks=%d | inference_workers=%d | model=%s | cache_ttl=%ds",
        len(config.carparks),
        config.inference_workers,
        config.model_path,
        config.request_cache_ttl_s,
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="smartpark-api", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: inject the user/request UUID into the logging context and emit a
# per-request access log line (timestamp | level | service | [uuid] | message).
# ---------------------------------------------------------------------------
@app.middleware("http")
async def uuid_context(request, call_next):
    uid = resolve_uuid(request.query_params.get("uuid"))
    token = current_uuid.set(uid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.info(
            "HTTP %s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            getattr(response, "status_code", "?"),
            elapsed_ms,
        )
        current_uuid.reset(token)
    return response


# ---------------------------------------------------------------------------
# Health / landing
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "smartpark-api",
        "status": "ok",
        "carparks": len(app.state.config.carparks),
    }


@app.get("/healthz")
def healthz():
    """Liveness/readiness probe for GKE."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _analyze_carpark(carpark: CarPark) -> dict | None:
    """Pull one image from a car park camera and run inference.

    Returns a result dict, or None if that car park's camera failed (so one bad
    camera never fails the whole /find-carparks request). Overlaps I/O (image
    fetch) with CPU (YOLO) because these tasks run concurrently under gather().
    """
    try:
        image = await fetch_image(
            app.state.http, carpark, app.state.config.takephoto_timeout_s
        )
    except TakephotoError as exc:
        log.warning("carpark %s camera error: %s", carpark.id, exc)
        return None
    analysis = await app.state.detector.analyze(image)
    return {
        "carpark_id": carpark.id,
        "name": carpark.name,
        "available_spaces": analysis["available_spaces"],
        "confidence_score": round(analysis["confidence_score"], 3),
    }


# ---------------------------------------------------------------------------
# CORE-API-1: top n car parks with the most available spaces
# ---------------------------------------------------------------------------
@app.get("/api/find-carparks")
async def find_carparks(
    uuid: str | None = Query(default=None, description="user uuid"),
    n: int = Query(default=3, ge=1, description="how many car parks to return"),
):
    uid = resolve_uuid(uuid)
    carparks = list(app.state.config.carparks)
    if not carparks:
        return JSONResponse(
            status_code=503, content={"status": "error", "msg": "no car parks configured"}
        )

    # What-if: n > 100 (or n > number of car parks). We simply cannot return more
    # results than the car parks we have, so clamp n. This is the robustness
    # answer to "what if the user sends a large n".
    eff_n = min(n, len(carparks))
    # To yield eff_n results we must query at least 2*eff_n random car parks.
    sample_n = min(2 * eff_n, len(carparks))
    chosen = random.sample(carparks, sample_n)

    # Repeated requests from the same user can be cached (per-pod).
    cache_key = (uid, eff_n)
    cached = app.state.cache.get(cache_key)
    if cached is not None:
        cached["msg"] = "success (cached)"
        return cached

    start = time.perf_counter()
    # Pull + infer all sampled car parks concurrently.
    results = await asyncio.gather(*(_analyze_carpark(cp) for cp in chosen))
    ok = [r for r in results if r is not None]
    ok.sort(key=lambda r: r["available_spaces"], reverse=True)
    top = ok[:eff_n]

    elapsed_ms = (time.perf_counter() - start) * 1000
    payload = {
        "uuid": uid,
        "status": "success",
        "msg": "success",
        "speed_inference": f"{elapsed_ms:.0f} ms",
        "requested_n": eff_n,
        "sampled_carparks": len(chosen),
        "results": top,
    }
    app.state.cache.set(cache_key, payload)
    log_request(time.time(), uid, "find-carparks")
    return payload


# ---------------------------------------------------------------------------
# CORE-API-2: annotated image for a specific car park
# ---------------------------------------------------------------------------
@app.get("/api/annotate-carpark")
async def annotate_carpark(
    carpark_id: str = Query(..., description="car park id, e.g. CBD_001"),
    uuid: str | None = Query(default=None, description="optional user uuid"),
):
    uid = resolve_uuid(uuid)
    carpark = app.state.config.carpark_by_id(carpark_id)
    if carpark is None:
        return JSONResponse(
            status_code=404,
            content={
                "carpark_id": carpark_id,
                "status": "error",
                "msg": f"unknown carpark_id {carpark_id}",
            },
        )
    try:
        image = await fetch_image(
            app.state.http, carpark, app.state.config.takephoto_timeout_s
        )
    except TakephotoError as exc:
        return JSONResponse(
            status_code=502,
            content={"carpark_id": carpark_id, "status": "error", "msg": str(exc)},
        )

    analysis = await app.state.detector.analyze(image)
    b64 = base64.b64encode(analysis["annotated_png"]).decode("utf-8")
    log_request(time.time(), uid, "annotate-carpark")

    return {
        "carpark_id": carpark_id,
        "status": "success",
        "msg": "success",
        "available_spaces": analysis["available_spaces"],
        "confidence_score": round(analysis["confidence_score"], 3),
        "image_base64": b64,
    }


# ---------------------------------------------------------------------------
# OPS-API-1: list all car parks and their current free spaces
# ---------------------------------------------------------------------------
@app.get("/api/ops/carparks")
async def ops_carparks():
    carparks = list(app.state.config.carparks)
    # Note: running inference on every car park is heavy; acceptable for an
    # operator endpoint. A short per-car park cache could be added.
    results = await asyncio.gather(*(_analyze_carpark(cp) for cp in carparks))
    rows = [r for r in results if r is not None]
    return {"status": "success", "count": len(rows), "carparks": rows}


# ---------------------------------------------------------------------------
# OPS-API-2: distinct users in the last 30 seconds (derived from OPS-REQ-1 logs)
# ---------------------------------------------------------------------------
@app.get("/api/ops/users")
async def ops_users():
    return {
        "status": "success",
        "users_last_30s": count_unique_users(30.0),
        "window_seconds": 30,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    log.info("Starting smartpark-api on 0.0.0.0:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
