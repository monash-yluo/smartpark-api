"""Car park configuration loader.
车场配置加载器.

The assignment requires the platform to discover its car parks from a single
source of truth (10-99 of them). We keep that single source as a JSON file and
load it at startup. On GKE the same JSON becomes a ConfigMap mounted as a volume
into every replica, so a single edit updates the whole cluster.

作业要求平台从单一数据源(10-99 个)发现车场.我们用一份 JSON 作为单一数据源,
在启动时加载.在 GKE 上,这份 JSON 会变成 ConfigMap 挂载成卷到每个副本,
因此只需改一处即可更新整个集群.

The application reads these environment variables (all optional):
应用会读取以下环境变量(均可选):
  CARPARKS_CONFIG   Path to carparks.json        (default: ./config/carparks.json)
  CARPARKS_COUNT    Number of car parks          (used only by the generator)
  TAKEPHOTO_URL     Base takephoto URL           (used only by the generator)

so the container image never hard-codes where the config lives or which
take-photo service to hit - the operator supplies them at runtime.
因此镜像从不硬编码配置位置或摄像头服务地址--由运维在运行时提供.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .logging_utils import log

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "carparks.json"


@dataclass(frozen=True)
class CarPark:
    """One car park the platform knows how to query.
    平台知道如何查询的一个车场."""

    id: str
    name: str
    takephoto_url: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "takephoto_url": self.takephoto_url}


@dataclass(frozen=True)
class PlatformConfig:
    """Parsed application configuration (car parks + runtime knobs).
    解析后的应用配置(车场清单 + 运行时参数)."""

    carparks: tuple[CarPark, ...]
    model_path: Path
    takephoto_timeout_s: float = 10.0
    inference_workers: int = 4
    request_cache_ttl_s: int = 30
    request_cache_refresh_after_s: int = 20

    def carpark_by_id(self, carpark_id: str) -> CarPark | None:
        # Look up a single car park by its id. 按 id 查找单个车场.
        for cp in self.carparks:
            if cp.id == carpark_id:
                return cp
        return None


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"Car park config not found at {path}. Set CARPARKS_CONFIG or "
            "make sure config/carparks.json is mounted (GKE ConfigMap)."
            # 车场配置文件未找到.请设置 CARPARKS_CONFIG,或确保 config/carparks.json
            # 已被挂载(GKE ConfigMap).
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_platform_config() -> PlatformConfig:
    """Load the car park list and runtime knobs from the environment + JSON file.
    从环境变量和 JSON 文件中加载车场清单与运行时参数."""
    config_path = Path(os.getenv("CARPARKS_CONFIG", DEFAULT_CONFIG_PATH)).resolve()
    raw = _read_json(config_path)

    carparks_raw = raw.get("carparks")
    if not carparks_raw:
        raise ValueError(f"No 'carparks' list found in {config_path}")

    carparks = tuple(
        CarPark(
            id=str(item["id"]).strip(),
            name=str(item.get("name", item["id"])).strip(),
            takephoto_url=str(item["takephoto_url"]).strip(),
        )
        for item in carparks_raw
    )
    if len(carparks) < 10:
        log.warning("Car park count is %s (< 10); assignment expects 10-99.", len(carparks))
        # 车场数量 %s(< 10);作业期望 10-99 个.

    model_path = Path(os.getenv("MODEL_PATH", "/models/model.pt")).resolve()

    request_cache_ttl_s = int(os.getenv("REQUEST_CACHE_TTL", "30"))
    request_cache_refresh_after_s = int(
        os.getenv("REQUEST_CACHE_REFRESH_AFTER", "20")
    )
    if not 0 < request_cache_refresh_after_s < request_cache_ttl_s:
        raise ValueError(
            "REQUEST_CACHE_REFRESH_AFTER must be greater than 0 and less than "
            "REQUEST_CACHE_TTL"
        )

    return PlatformConfig(
        carparks=carparks,
        model_path=model_path,
        takephoto_timeout_s=float(os.getenv("TAKEPHOTO_TIMEOUT", "10")),
        inference_workers=max(1, int(os.getenv("INFERENCE_WORKERS", "4"))),
        request_cache_ttl_s=request_cache_ttl_s,
        request_cache_refresh_after_s=request_cache_refresh_after_s,
    )
