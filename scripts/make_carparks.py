"""Generate the car park list (single source of truth) used by the platform.
生成平台使用的车场清单(单一数据源).

The assignment says the platform discovers 10-99 car parks from a single source
of truth, and each car park exposes an API: /api/takephoto. We keep that list as
config/carparks.json. On GKE this same JSON becomes a ConfigMap mounted into every
replica (see k8s/configmap.yaml), so the operator edits one place.
作业规定平台从单一数据源发现 10-99 个车场,且每个车场暴露一个 API:/api/takephoto.
我们把这份清单保存在 config/carparks.json.在 GKE 上,同一份 JSON 会变成 ConfigMap
挂载到每个副本(见 k8s/configmap.yaml),因此运维只需改一处.

Env vars (optional):
  CARPARKS_COUNT   how many car parks to generate   (default 30)
  TAKEPHOTO_URL    base camera endpoint             (default = deployed Cloud Run)
环境变量(可选):
  CARPARKS_COUNT   生成多少个车场   (默认 30)
  TAKEPHOTO_URL    摄像头基础地址     (默认 = 已部署的 Cloud Run)
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TAKEPHOTO_URL = (
    "https://takephoto-api-87185953953.australia-southeast2.run.app/api/takephoto"
)


def generate(count: int, takephoto_url: str) -> dict:
    carparks = []
    for i in range(1, count + 1):
        cid = f"CBD_{i:03d}"
        carparks.append(
            {"id": cid, "name": f"Car Park {cid}", "takephoto_url": takephoto_url}
        )
    return {"carparks": carparks}


def main() -> None:
    count = int(os.getenv("CARPARKS_COUNT", "30"))
    url = os.getenv("TAKEPHOTO_URL", DEFAULT_TAKEPHOTO_URL)
    data = generate(count, url)
    out = BASE_DIR / "config" / "carparks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {out} with {count} car parks; takephoto_url={url}")


if __name__ == "__main__":
    main()
