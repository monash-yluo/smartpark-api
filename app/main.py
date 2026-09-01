"""SmartPark main platform API.
SmartPark 主平台 API.

A single FastAPI service that is horizontally scaled on GKE (1/2/4/8 replicas)
behind a LoadBalancer and HPA. This is the graded object - it is what Locust
hammers. It is a MONOLITH (not microservices): one service, many replicas.
一个在 GKE 上水平扩展(1/2/4/8 副本)的单一 FastAPI 服务,位于 LoadBalancer 和 HPA 之后.
这是被评分对象--也就是 Locust 压测的目标.它是单体(不是微服务):一个服务,多个副本.

Endpoints (per assignment):
  CORE-API-1  GET /api/find-carparks?uuid=...&n=3     top n car parks by free spaces
  CORE-API-2  GET /api/annotate-carpark?carpark_id=   annotated image, base64
  OPS-API-1   GET /api/ops/carparks                  list all car parks + free spaces
  OPS-API-2   GET /api/ops/users                     distinct users in last 30s
端点(按作业):
  CORE-API-1  GET /api/find-carparks?uuid=...&n=3     按空位数返回前 n 个车场
  CORE-API-2  GET /api/annotate-carpark?carpark_id=   标注图,base64
  OPS-API-1   GET /api/ops/carparks                  列出所有车场 + 空位数
  OPS-API-2   GET /api/ops/users                     最近 30 秒的不同用户数

OPS-REQ-2 (operational dashboard) is intentionally deferred to a later step; the
module header in app/dashboard.py (not yet created) will hold it. The logging
requirement (OPS-REQ-1) is handled by logging_utils + the middleware below.
OPS-REQ-2(运营仪表盘)有意推迟到后续步骤;app/dashboard.py(尚未创建)的模块头将承载它.
日志要求(OPS-REQ-1)由 logging_utils + 下方中间件处理.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .cache import TTLCache
from .config import CarPark, load_platform_config
from .inference import build_detector
from .logging_utils import (
    build_user_id,
    count_unique_users,
    current_uuid,
    get_client_ip,
    log,
    log_request,
    resolve_uuid,
)
from .takephoto import TakephotoError, fetch_image


async def _shutdown_inflight_analyses(app: FastAPI) -> None:
    """优雅停止所有正在进行的车场图片分析任务。 / Gracefully stop all in-flight carpark image analysis tasks."""
    async with app.state.inflight_analyses_lock:
        # 获取所有正在进行的分析任务并清空字典,以便在应用关闭时不再接受新任务. / Get all in-flight analysis tasks and clear the dictionary so that no new tasks are accepted when the app is shutting down.
        tasks = list(app.state.inflight_analyses.values())
        app.state.inflight_analyses.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    # 等待所有正在关闭的分析 Task 都真正结束 / Wait for all shutting down analysis tasks to actually finish
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Lifespan: load config, model, cache, HTTP client once at startup
# 生命周期:启动时一次性加载配置,模型,缓存,HTTP 客户端
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化应用资源，并在服务退出时按顺序清理共享任务和 HTTP 客户端。 / Initialize application resources and clean up shared tasks and HTTP client in order on service exit."""
    # 加载本地config
    config = load_platform_config()
    app.state.config = config
    app.state.cache = TTLCache(default_ttl=config.request_cache_ttl_s)
    app.state.inflight_analyses = {}
    app.state.inflight_analyses_lock = asyncio.Lock()
    # The model is loaded from disk at runtime (MODEL_PATH). If it is not
    # available (e.g. local dev), build_detector returns a MockDetector so the
    # endpoints still boot and can be tested. On GKE, MODEL_PATH is a mounted PVC.
    # 模型在运行时从磁盘加载(MODEL_PATH).若不可用(例如本地开发),
    # build_detector 会返回 MockDetector,使端点仍能启动并测试.在 GKE 上 MODEL_PATH 是挂载的 PVC.
    app.state.detector = build_detector(config.model_path, config.inference_workers)
    # 异步 HTTP 客户端 主动去调 takephoto（Cloud Run）拉图
    app.state.http = httpx.AsyncClient(timeout=config.takephoto_timeout_s)

    log.info(
        "started | carparks=%d | inference_workers=%d | model=%s | cache_ttl=%ds",
        len(config.carparks),
        config.inference_workers,
        config.model_path,
        config.request_cache_ttl_s,
    )

    yield
    
    # 优雅关闭:取消所有正在进行的分析任务并关闭 HTTP 客户端. / Graceful shutdown: cancel all in-flight analyses and close the HTTP client. /
    try:
        await _shutdown_inflight_analyses(app)
    finally:
        await app.state.http.aclose()


app = FastAPI(title="smartpark-api", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: inject the user/request UUID into the logging context and emit a
# per-request access log line (timestamp | level | service | [uuid] | message).
# 中间件:将用户/请求 UUID 注入日志上下文,并输出每条请求的访问日志行.
#
# @app.middleware("http") wraps the ASGI app, which sits at the APPLICATION layer.
# TLS/HTTPS is a TRANSPORT-layer concern that is handled before this app sees the
# request: locally uvicorn terminates TLS; in GKE the Ingress/LoadBalancer does.
# So this middleware runs identically for http and https -- it only ever sees the
# already-decrypted HTTP request.
# @app.middleware("http") 包装的是应用层的 ASGI 应用.TLS/HTTPS 是传输层的事,
# 由应用之前的一端处理:本地是 uvicorn 终止 TLS;GKE 上是 Ingress/LoadBalancer.
# 因此该中间件对 http 和 https 一视同仁--它只会看到已解密的 HTTP 请求.
#
# We store the uuid in a ContextVar so that all logs emitted while handling this
# request (a separate asyncio task with its own context) carry that uuid.
# 我们把 uuid 存入一个 ContextVar,使该请求(一个拥有独立上下文的 asyncio 任务)
# 处理期间发出的所有日志都带上这个 uuid.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def uuid_context(request, call_next):
    """为请求设置稳定的用户标识，并记录请求方法、路径、状态和耗时。 / Set a stable user identifier for the request and log the request method, path, status, and elapsed time."""
    # Resolve a stable "user id": the client uuid if provided, else the client IP
    # (so one user hitting the API multiple times groups to one id, not many).
    # 解析一个稳定的"用户 id":优先用客户端 uuid,否则用客户端 IP
    # (这样同一用户多次调用会归并为一个 id,而不会变成多个).
    client_ip = get_client_ip(request)
    request.state.client_ip = client_ip
    user_id = build_user_id(request.query_params.get("uuid"), client_ip)
    request.state.user_id = user_id
    # set() the user id into THIS request's context and keep the Token so we can
    # reset() it afterwards (restores the previous value, avoids leaking it).
    # 把用户 id 写入"当前请求"的上下文,并保留 Token 以便之后 reset()(恢复旧值,避免泄漏).
    token = current_uuid.set(user_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        # 计算时间 / Calculate elapsed time
        elapsed_ms = (time.perf_counter() - start) * 1000
        # 记录访问日志:HTTP 方法,路径,状态码,耗时 / Log access: HTTP method, path, status code, elapsed time 
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
# Health / landing  健康 / 信息
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    """返回服务名称、运行状态和当前已加载的车场数量。 / Return the service name, running status, and the number of currently loaded carparks."""
    return {
        "service": "smartpark-api",
        "status": "ok",
        "carparks": len(app.state.config.carparks),
    }


@app.get("/healthz")
def healthz():
    """Liveness/readiness probe for GKE.
    GKE 的存活/就绪探针。"""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers  辅助函数
# ---------------------------------------------------------------------------
async def _load_and_cache_carpark_analysis(carpark: CarPark) -> dict | None:
    """获取一个车场的摄像头图片，执行推理，并缓存成功的分析结果。 / Fetch an image from a carpark's camera, perform inference, and cache the successful analysis results."""
    cache_key = ("carpark-analysis", carpark.id)

    try:
        image = await fetch_image(
            app.state.http, carpark, app.state.config.takephoto_timeout_s
        )
    except TakephotoError as exc:
        log.warning("carpark %s camera error: %s", carpark.id, exc)
        return None

    try:
        analysis = await app.state.detector.analyze(image)
    except Exception as exc:  # noqa: BLE001 - a single bad photo must not fail the whole request
        log.warning("carpark %s analyze error: %s", carpark.id, exc)
        return None

    app.state.cache.set(cache_key, analysis)
    return analysis


async def _get_carpark_analysis(carpark: CarPark) -> dict | None:
    """返回缓存中或正在执行的分析结果；必要时创建一个共享任务。

    Return a cached or in-flight analysis, starting one task when needed.
    从某个车场摄像头拉取一张图片并运行推理.

    Successful analyses are cached by car park ID, so every endpoint can reuse
    the same count, confidence, and annotated image during the TTL window.
    成功的分析结果按停车场 ID 缓存,因此所有端点都能在 TTL 内复用同一份计数,
    置信度和标注图.

    Returns a full analysis dict, or None if that car park's camera or inference
    failed (so one bad camera never fails the whole /find-carparks request).
    Overlaps I/O (image fetch) with CPU (YOLO) because these tasks run
    concurrently under gather().
    返回结果字典;如果该车场摄像头失败则返回 None(这样单个摄像头故障不会让整个
    /find-carparks 请求失败).由于这些任务在 gather() 下并发运行,I/O(拉图)与 CPU(YOLO)重叠.
    """
    # 以停车场 ID 缓存完整分析结果，供多个接口在 TTL 内复用。 / Cache the full analysis by car park ID for reuse by multiple endpoints during the TTL.
    cache_key = ("carpark-analysis", carpark.id)
    cached = app.state.cache.get(cache_key)
    if cached is not None:
        return cached

    async with app.state.inflight_analyses_lock:
        # 等待锁时,另一个请求可能已经完成分析并写入缓存. / Another request may have completed the analysis while this request was waiting for the lock.
        cached = app.state.cache.get(cache_key)
        if cached is not None:
            return cached

        task = app.state.inflight_analyses.get(carpark.id)
        if task is None:
            task = asyncio.create_task(_load_and_cache_carpark_analysis(carpark))
            app.state.inflight_analyses[carpark.id] = task

            def remove_completed_task(completed_task: asyncio.Task) -> None:
                if app.state.inflight_analyses.get(carpark.id) is completed_task:
                    del app.state.inflight_analyses[carpark.id]

            task.add_done_callback(remove_completed_task)

    # 已取消的 HTTP 请求不能取消共享的拉图/推理任务. / A cancelled HTTP request must not cancel the shared camera/inference task.
    return await asyncio.shield(task)


async def _analyze_carpark(carpark: CarPark) -> dict | None:
    """根据完整分析结果生成 CORE-API 使用的车场摘要。"""
    analysis = await _get_carpark_analysis(carpark)
    if analysis is None:
        return None
    return {
        "carpark_id": carpark.id,
        "name": carpark.name,
        "available_spaces": analysis["available_spaces"],
        "confidence_score": round(analysis["confidence_score"], 3),
    }


# ---------------------------------------------------------------------------
# CORE-API-1: top n car parks with the most available spaces
# CORE-API-1:空位数最多的前 n 个车场
# ---------------------------------------------------------------------------
@app.get("/api/find-carparks")
async def find_carparks(
    request: Request,
    # uuid is REQUIRED by CORE-API-1: "each simulated user has a uuid to associate
    # the request with the response." FastAPI enforces presence + length and returns
    # 422 (validation error) when it is missing / blank / too long.
    # uuid 是 CORE-API-1 的必填参数(作业:每个模拟用户都带 uuid 关联请求与响应).
    # 由 FastAPI 校验:缺失 / 空 / 超长时返回 422(校验错误).
    uuid: str = Query(
        ..., min_length=1, max_length=64, description="required user uuid"
    ),
    n: int = Query(default=3, ge=1, description="how many car parks to return"),
):
    """随机查询车场并返回可用车位最多的前 n 个结果。"""
    # raw uuid to echo back to the client (matches the spec output format).
    # 回显给客户端的原始 uuid(对应作业输出格式).
    raw_uuid = resolve_uuid(uuid)
    # the stable grouping id set by the middleware (user:<uuid> or ip:<client-ip>).
    # 中间件设置好的稳定分组 id(user:<uuid> 或 ip:<client-ip>).
    user_id = request.state.user_id
    carparks = list(app.state.config.carparks)
    if not carparks:
        return JSONResponse(
            status_code=503, content={"status": "error", "msg": "no car parks configured"}
        )

    # What-if: n > 100 (or n > number of car parks). We simply cannot return more
    # results than the car parks we have, so clamp n. This is the robustness
    # answer to "what if the user sends a large n".
    # 边界情况:n > 100(或 n > 车场数量).我们无法返回超过现有车场的数量,所以将 n 钳制.
    # 这就是对"如果用户发送很大的 n"这一问题的健壮性回答.
    eff_n = min(n, len(carparks))
    # To yield eff_n results we must query at least 2*eff_n random car parks.
    # 要得出 eff_n 个结果,我们必须随机查询至少 2*eff_n 个车场.
    sample_n = min(2 * eff_n, len(carparks))
    chosen = random.sample(carparks, sample_n)

    start = time.perf_counter()
    # Pull + infer all sampled car parks concurrently.
    # 并发拉取并推理所有被采样车场.
    results = await asyncio.gather(*(_analyze_carpark(cp) for cp in chosen))
    ok = [r for r in results if r is not None]
    # If every sampled car park failed (camera down / inference error), we cannot
    # produce any results. Return 503 instead of pretending there are simply no
    # free spaces (which would be misleading to callers).
    # 如果所有被采样车场都失败(摄像头宕机/推理错误),我们无法产生任何结果.
    # 此时返回 503,而不是假装"车场恰好没有空位"(那会误导调用方).
    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "uuid": raw_uuid,
                "status": "error",
                "msg": "all sampled car parks are unavailable",
                "sampled_carparks": len(chosen),
                "results": [],
            },
        )
    ok.sort(key=lambda r: r["available_spaces"], reverse=True)
    top = ok[:eff_n]

    elapsed_ms = (time.perf_counter() - start) * 1000
    payload = {
        "uuid": raw_uuid,
        "status": "success",
        "msg": "success",
        "speed_inference": f"{elapsed_ms:.0f} ms",
        "requested_n": eff_n,
        "sampled_carparks": len(chosen),
        "failed_carparks": len(chosen) - len(ok),
        "results": top,
    }
    log_request(time.time(), user_id, "find-carparks")
    return payload


# ---------------------------------------------------------------------------
# CORE-API-2: annotated image for a specific car park
# CORE-API-2:某个具体车场的标注图
# ---------------------------------------------------------------------------
@app.get("/api/annotate-carpark")
async def annotate_carpark(
    request: Request,
    carpark_id: str = Query(..., description="car park id, e.g. CBD_001"),
    uuid: str | None = Query(default=None, description="optional user uuid"),
):
    """获取指定车场的分析结果，并返回其 Base64 编码的标注图片。"""
    # middleware-derived stable id (user:<uuid>, else ip:<client-ip>).
    # 中间件得到的稳定 id(user:<uuid>,否则 ip:<client-ip>).
    user_id = request.state.user_id
    carpark = app.state.config.carpark_by_id(carpark_id)

    # 如果没有找到对应的车场,返回 404 错误
    if carpark is None:
        return JSONResponse(
            status_code=404,
            content={
                "carpark_id": carpark_id,
                "status": "error",
                "msg": f"unknown carpark_id {carpark_id}",
            },
        )

    # Once the car park exists this is a valid CORE-API-2 call. Record it BEFORE
    # the work so even a failed fetch/analysis still counts the user as active
    # for OPS-API-2 (distinct users in last 30s).
    # 车场存在后即为一次有效的 CORE-API-2 调用.在处理前记录,这样即使拉图/分析失败,
    # 该用户仍会计入 OPS-API-2(最近 30 秒不同用户数).
    log_request(time.time(), user_id, "annotate-carpark")

    analysis = await _get_carpark_analysis(carpark)
    if analysis is None:
        return JSONResponse(
            status_code=502,
            content={
                "carpark_id": carpark_id,
                "status": "error",
                "msg": "image analysis failed",
            },
        )
    b64 = base64.b64encode(analysis["annotated_png"]).decode("utf-8")

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
# OPS-API-1:列出所有车场及其当前空位数
# ---------------------------------------------------------------------------
@app.get("/api/ops/carparks")
async def ops_carparks():
    """查询所有已配置车场，并返回各车场当前的可用车位数。"""
    carparks = list(app.state.config.carparks)
    # Note: running inference on every car park is heavy; acceptable for an
    # operator endpoint. A short per-car park cache could be added.
    # 注意:对每个车场跑推理较消耗资源;作为运营端点可接受.可增加短时的按车场缓存.
    results = await asyncio.gather(*(_analyze_carpark(cp) for cp in carparks))
    rows = [r for r in results if r is not None]
    return {"status": "success", "count": len(rows), "carparks": rows}


# ---------------------------------------------------------------------------
# OPS-API-2: distinct users in the last 30 seconds (derived from OPS-REQ-1 logs)
# OPS-API-2:最近 30 秒内的不同用户数(由 OPS-REQ-1 日志推导)
# ---------------------------------------------------------------------------
@app.get("/api/ops/users")
async def ops_users():
    """统计最近 30 秒访问过有效 API 的不同用户数量。"""
    return {
        "status": "success",
        "users_last_30s": count_unique_users(30.0),
        "window_seconds": 30,
    }


# ---------------------------------------------------------------------------
# Entry point  入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    log.info("Starting smartpark-api on 0.0.0.0:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
