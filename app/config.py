"""Car park configuration loader.

The assignment requires the platform to *discover* its car parks from a single
source of truth (10-99 of them). We keep that single source as a JSON file and
load it at startup. On GKE the same JSON becomes a ConfigMap that is mounted as
a volume into every replica, so a single edit updates the whole cluster — see
k8s/configmap.yaml.

The application reads three environment variables (all optional):

  CARPARKS_CONFIG   Path to carparks.json        (default: ./config/carparks.json)
  CARPARKS_COUNT    Number of car parks          (used only by the generator)
  TAKEPHOTO_URL     Base takephoto URL           (used only by the generator)

so the container image never hard-codes where the config lives or which
take-photo service to hit — the operator supplies them at runtime.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("smartpark.config")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "carparks.json"


@dataclass(frozen=True)
class CarPark:
    """One car park the platform knows how to query."""

    id: str
    name: str
    takephoto_url: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "takephoto_url": self.takephoto_url}


@dataclass(frozen=True)
class PlatformConfig:
    """Parsed application configuration (car parks + runtime knobs)."""

    carparks: tuple[CarPark, ...]
    model_path: Path
    takephoto_timeout_s: float = 10.0
    inference_workers: int = 4
    request_cache_ttl_s: int = 30

    def carpark_by_id(self, carpark_id: str) -> CarPark | None:
        for cp in self.carparks:
            if cp.id == carpark_id:
                return cp
        return None


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"Car park config not found at {path}. Set CARPARKS_CONFIG or "
            "make sure config/carparks.json is mounted (GKE ConfigMap)."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_platform_config() -> PlatformConfig:
    """Load the car park list and runtime knobs from the environment + JSON file."""
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

    model_path = Path(os.getenv("MODEL_PATH", "/models/model.pt")).resolve()

    return PlatformConfig(
        carparks=carparks,
        model_path=model_path,
        takephoto_timeout_s=float(os.getenv("TAKEPHOTO_TIMEOUT", "10")),
        inference_workers=max(1, int(os.getenv("INFERENCE_WORKERS", "4"))),
        request_cache_ttl_s=int(os.getenv("REQUEST_CACHE_TTL", "30")),
    )
