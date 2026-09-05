# SmartPark — Main Platform API

The graded, user-facing FastAPI service for FIT3184 2026 S2 Assignment 1.
It is a **single monolith** that is horizontally scaled (1/2/4/8 replicas) on GKE
behind a LoadBalancer and a CPU-based HPA. Locust hammers this service.

It is **not** the takephoto camera simulator (that is the separate small service
already deployed to Cloud Run; this platform pulls images from it).

## Endpoints

| API | Method + Path | Purpose |
|-----|---------------|---------|
| CORE-API-1 | GET /api/find-carparks?uuid=..&n=3 | Top n car parks by available spaces |
| CORE-API-2 | GET /api/annotate-carpark?carpark_id=.. | Annotated image (base64) |
| OPS-API-1  | GET /api/ops/carparks | All car parks + current free spaces |
| OPS-API-2  | GET /api/ops/users | Distinct users in the last 30 s |
| probe      | GET /healthz, GET /                     | Liveness / info |

OPS-REQ-1 (request logging) is handled by the middleware + app/logging_utils.py.
OPS-REQ-2 (operational dashboard) is available at `/dashboard/`. It is served by
FastAPI and uses Plotly.js in the browser. Car park telemetry refreshes every 10
seconds and active-user telemetry refreshes every 5 seconds; the two data sources
degrade independently when an operational dependency is unavailable.

## Project layout

    smartpark-api/
    ├─ app/
    │  ├─ main.py          # FastAPI app + all routes + lifespan
    │  ├─ config.py        # load car parks from carparks.json (single source of truth)
    │  ├─ takephoto.py     # async HTTP client for the camera service
    │  ├─ inference.py     # YOLO wrapper (runtime-loaded model, thread-pool async)
   │  ├─ cache.py         # per-car-park TTL cache for inference results
   │  ├─ dashboard.py     # operational dashboard route
   │  ├─ static/          # dashboard HTML, CSS, and Plotly.js client code
   │  └─ logging_utils.py # structured logging + uuid context + request log
    ├─ config/carparks.json  # car park list (generated; becomes a GKE ConfigMap)
    ├─ scripts/make_carparks.py  # regenerate carparks.json
    ├─ requirements.txt
    ├─ Dockerfile          # lightweight; model weights NOT baked in
    ├─ .dockerignore / .gitignore
    └─ README.md

## Run locally

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt   # note: also installs ultralytics
    .venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

If the model is not present the app falls back to a MockDetector so the endpoints
still run (CPU counts are deterministic stand-ins). To use real YOLO, set
MODEL_PATH to the model.pt / model.onnx file.

## Configuration (env vars)

- CARPARKS_CONFIG  path to carparks.json          (default ./config/carparks.json)
- MODEL_PATH       path to the model weights      (default /models/model.pt)
- TAKEPHOTO_TIMEOUT  seconds for camera calls     (default 10)
- INFERENCE_WORKERS  YOLO thread-pool size        (default 4)
- REQUEST_CACHE_TTL  per-car-park analysis cache TTL seconds   (default 30)
- FIRESTORE_DATABASE Firestore database ID (default `(default)`)
- PORT             listen port                    (default 8000)

## Key design decisions (worth explaining in the interview)

1. Car park discovery = single source of truth. The platform loads car parks from
   carparks.json at startup. On GKE that JSON is a ConfigMap mounted into every
   replica, so editing one place updates the whole cluster.
2. Updateable model. Weights are NOT copied into the image. On GKE, an
   initContainer downloads the selected GCS object into a shared `emptyDir`
   volume, and the app reads it through `MODEL_PATH=/data/model.pt`. Changing
   the model URI in the ConfigMap and restarting the Deployment makes new Pods
   load the new model without rebuilding the application image.
3. Inference does not block the event loop. YOLO is synchronous and CPU-bound, so
   it runs on a ThreadPoolExecutor via loop.run_in_executor; the route just awaits
   it (see app/inference.py). This is the core "bottleneck + mitigation" point.
4. 2n sampling + concurrency. find-carparks samples 2*n car parks and pulls +
   infers them concurrently (asyncio.gather), then returns the top n by free spaces.
5. Per-car-park inference cache. A successful analysis (counts, confidence, and
   annotated image) is cached by car-park ID for the TTL, so all endpoints reuse it.
6. Large n (what-if). n is clamped to the number of car parks, and at most 2*n are
   sampled, so a huge n (e.g. 200) cannot be abused to hit the cameras/replicas.
7. Known caveat. OPS-API-2 counts from an in-memory request log, so it is per-pod.
   Across replicas each pod sees only its own traffic; a cluster-wide count would
   need shared storage (Redis / Cloud Logging).
8. User identity: uuid with IP fallback. The platform needs a stable id so a user's
   requests group together (for logging and OPS-API-2 user counting). If the client
   supplies a uuid we use "user:<uuid>"; if not, we fall back to the client IP
   ("ip:<client_ip>"), so one user making many requests (e.g. several annotate
   calls) is counted once, not many times. The raw user uuid is still echoed back
   in find-carparks responses. Behind a proxy / load-balancer (GKE Ingress, Cloud
   Run) we read the real client from X-Forwarded-For (leftmost value), otherwise we
   use the direct peer address (see get_client_ip in app/logging_utils.py).
   NOTE: IP is a heuristic, not a true identity — NAT sharing under-counts, mobile
   IP churn can over-count, and X-Forwarded-For should only be trusted from known
   proxies.

## Next steps

- GKE manifests (namespace, ConfigMaps, node service-account IAM permission,
  Deployment, LoadBalancer Service, HPA) and real deployment. The model uses
  GCS + an initContainer + shared `emptyDir`, rather than a PVC.

## Runtime model deployment

The model file is intentionally absent from the Docker build context and image.
Upload it to a private GCS bucket, set `MODEL_URI` in
`k8s/model-configmap.yaml`, and grant the GKE node pool service account
`storage.objects.get` (or `roles/storage.objectViewer`). The Deployment does
not specify `serviceAccountName`; Pods use the namespace `default` ServiceAccount
and access GCS through the node identity in the current cluster setup.

### Generate the car-park ConfigMap

`carparks.json` is small enough for a ConfigMap. The source file remains in the
repository, while the command below creates or updates the Kubernetes resource
from that file:

```bash
kubectl create configmap smartpark-carparks \
   --from-file=carparks.json=config/carparks.json \
   --dry-run=client -o yaml | kubectl apply -f -
```

This ConfigMap is mounted by `k8s/deployment.yaml` at
`/app/config/carparks.json`. The application reads it during startup, so apply
the ConfigMap and restart the Deployment after changing `carparks.json`:

```bash
kubectl rollout restart deployment/smartpark-api
kubectl rollout status deployment/smartpark-api
```

### Test on a Compute Engine VM

The VM test deliberately keeps the model outside the image. The VM host first
downloads the model from GCS, then Docker bind-mounts the host directory into
the application container. The container reads `/data/model.pt` through
`MODEL_PATH`.

```bash
mkdir -p ~/smartpark-models
gcloud storage cp \
   gs://YOUR_BUCKET/model.pt \
   ~/smartpark-models/model.pt
ls -lh ~/smartpark-models/model.pt
test -s ~/smartpark-models/model.pt && echo "model download succeeded"
```

The equivalent helper script is executable through `bash` even when the file
does not have the Linux execute bit:

```bash
MODEL_URI=gs://YOUR_BUCKET/model.pt \
   bash scripts/download_model_vm.sh \
   ~/smartpark-models/model.pt
```

Build and run the image on the VM:

```bash
docker build -t smartpark-api:v1 .
docker run --rm \
   --name smartpark-api-test \
   -p 8000:8000 \
   -e MODEL_PATH=/data/model.pt \
   -v ~/smartpark-models:/data:ro \
   smartpark-api:v1
```

The `-v` option makes the VM host file visible inside the container; it does
not copy the model into the image. In a second VM terminal, verify the model
file, health endpoint, and detector mode:

```bash
docker exec smartpark-api-test ls -lh /data/model.pt
curl http://127.0.0.1:8000/healthz
docker logs smartpark-api-test 2>&1 | grep -Ei 'loading yolo|real yolo|mock|could not load|detector'
```

The startup log must contain:

```text
Using REAL YOLO detector from /data/model.pt
detector=real-yolo
```

`detector=mock` means that the model file was not found or could not be loaded.
The Base64 image length alone cannot distinguish real YOLO from MockDetector;
MockDetector returns the original image without drawing detection boxes.

Build and push the lightweight image to Artifact Registry:

```bash
gcloud auth configure-docker REGION-docker.pkg.dev
docker build -t REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG .
docker push REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG
```

The Kubernetes Deployment replaces `REGION`, `PROJECT_ID`, and `TAG` in its
image field. The initContainer downloads one model per new Pod into `emptyDir`;
changing `MODEL_URI` therefore requires `kubectl rollout restart deployment/smartpark-api`.

Create the car-park ConfigMap referenced by `k8s/deployment.yaml`, then apply the
model configuration and Deployment:

```bash
kubectl create configmap smartpark-carparks \
   --from-file=carparks.json=config/carparks.json \
   --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/model-configmap.yaml
kubectl apply -f k8s/deployment.yaml
```
- Locust script + benchmark report for 1/2/4/8 replicas.
