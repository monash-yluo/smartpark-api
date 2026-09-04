"""Async client for the car park camera (takephoto) service.
车场摄像头(takephoto)服务的异步客户端.

The camera does NOT push video to us. Instead the assignment says the platform
sends an HTTP GET to the camera to request an image (the "pull" model). The
takephoto service you already built returns a base64-encoded random dataset
image. This module turns that response into raw image bytes and packages a
convenient, concurrency-friendly way to pull many images in parallel.
摄像头并不会把视频推给我们.作业规定平台向摄像头发送 HTTP GET 请求拉取图片("拉"模型).
你已写好的 takephoto 服务会返回一张 base64 编码的随机数据集图片.本模块把该响应
转成原始图片字节,并提供一种方便,利于并发的方式同时拉取多张图片.
"""

from __future__ import annotations

import base64
from typing import Optional

import httpx

from .config import CarPark
from .logging_utils import log


class TakephotoError(RuntimeError):
    """Raised when the camera service returns an unusable response.
    当摄像头服务返回不可用的响应时抛出."""


async def fetch_image(
    client: httpx.AsyncClient,
    carpark: CarPark,
    timeout: float = 10.0,
) -> bytes:
    """Request one image from a car park's camera and return raw decoded bytes.
    向某个车场摄像头请求一张图片,并返回解码后的原始字节.

    The takephoto endpoint is addressed as <carpark.takephoto_url>?carpark_id=<id>
    (the URL is stored without a query string; we append the id here). This keeps
    each car park's camera address as the single source of truth in config.
    请求地址为 <carpark.takephoto_url>?carpark_id=<id>(URL 本身不含查询串,我们在此追加 id).
    这样每个车场的摄像头地址是我们配置中的唯一数据源.
    """
    url = carpark.takephoto_url
    try:
        resp = await client.get(url, params={"carpark_id": carpark.id}, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise TakephotoError(
            f"takephoto HTTP error for {carpark.id}: {exc}"
        ) from exc

    try:
        payload = resp.json()
    except ValueError as exc:  # resp.json() raises ValueError on bad JSON
        # resp.json() 在 JSON 无效时会抛 ValueError.
        raise TakephotoError(
            f"takephoto returned non-JSON for {carpark.id}"
        ) from exc

    if payload.get("status") != "success":
        raise TakephotoError(
            f"takephoto status != success for {carpark.id}: {payload.get('msg')}"
        )

    image_b64 = payload.get("image_base64")
    if not image_b64:
        raise TakephotoError(f"takephoto missing image_base64 for {carpark.id}")

    try:
        return base64.b64decode(image_b64)
    except Exception as exc:  # noqa: BLE001
        raise TakephotoError(
            f"takephoto bad base64 for {carpark.id}: {exc}"
        ) from exc


async def fetch_many(
    client: httpx.AsyncClient,
    carparks: list[CarPark],
    timeout: float = 10.0,
) -> list[tuple[Optional[CarPark], Optional[bytes]]]:
    """Pull images for many car parks concurrently.
    并发拉取多个车场的图片.

    Returns a list of (carpark, bytes) pairs. On a per-car park failure the bytes
    entry is None (the caller decides whether to skip it) so one bad camera never
    breaks the whole request. This is what lets the platform keep going when a
    single camera is down - a resilience point worth explaining.
    返回 (车场, 字节) 列表.某个车场失败时其字节项为 None(由调用方决定是否跳过),
    这样单个摄像头故障不会破坏整个请求.这就是平台在单个摄像头宕机时仍能继续运行的原因
    --这是值得解释的韧性点.
    """
    async def _one(cp: CarPark) -> tuple[Optional[CarPark], Optional[bytes]]:
        try:
            return cp, await fetch_image(client, cp, timeout)
        except TakephotoError as exc:
            log.warning("skipping %s: %s", cp.id, exc)
            return cp, None

    return await gather(_one(cp) for cp in carparks)


# Simple wrapper so callers can await a generator of awaitables.
# 简单包装,使调用方可 await 一个可等待对象生成器.
async def gather(awaitables):
    import asyncio

    return await asyncio.gather(*awaitables)
