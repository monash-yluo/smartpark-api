"""Async client for the car park camera (takephoto) service.

The camera does NOT push video to us. Instead the assignment says the platform
sends an HTTP GET to the camera to request an image (the "pull" model). The
takephoto service you already built returns a base64-encoded random dataset
image. This module turns that response into raw image bytes and packages a
convenient, concurrency-friendly way to pull many images in parallel.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from .config import CarPark

log = logging.getLogger("smartpark.takephoto")


class TakephotoError(RuntimeError):
    """Raised when the camera service returns an unusable response."""


async def fetch_image(
    client: httpx.AsyncClient,
    carpark: CarPark,
    timeout: float = 10.0,
) -> bytes:
    """Request one image from a car park's camera and return raw decoded bytes.

    The takephoto endpoint is addressed as  <carpark.takephoto_url>?carpark_id=<id>
    (the URL is stored without a query string; we append the id here). This keeps
    each car park's camera address as the *single source of truth* in config.
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

    Returns a list of (carpark, bytes) pairs. On a per-car park failure the bytes
    entry is None (the caller decides whether to skip it) so one bad camera never
    breaks the whole request. This is what lets the platform keep going when a
    single camera is down — a resilience point worth explaining.
    """
    async def _one(cp: CarPark) -> tuple[Optional[CarPark], Optional[bytes]]:
        try:
            return cp, await fetch_image(client, cp, timeout)
        except TakephotoError as exc:
            log.warning("skipping %s: %s", cp.id, exc)
            return cp, None

    return await gather(_one(cp) for cp in carparks)


# Simple wrapper so callers can await a generator of awaitables.
async def gather(awaitables):
    import asyncio

    return await asyncio.gather(*awaitables)
