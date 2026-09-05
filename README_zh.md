# SmartPark —— 主平台 API

这是 FIT3184 2026 S2 Assignment 1 中经过评分、面向用户的 FastAPI 服务。
它是一个**单体应用**，在 GKE 上以 1/2/4/8 个副本进行水平扩展，部署在 LoadBalancer 和基于 CPU 的 HPA 后方。Locust 会对该服务进行压力测试。

它**不是** takephoto 摄像头模拟器（那是已经部署到 Cloud Run 的独立小型服务；本平台会从该服务拉取图片）。

## 接口

| API | 方法 + 路径 | 用途 |
|-----|---------------|---------|
| CORE-API-1 | GET /api/find-carparks?uuid=..&n=3 | 按可用车位数返回前 n 个停车场 |
| CORE-API-2 | GET /api/annotate-carpark?carpark_id=.. | 返回带标注的图片（Base64） |
| OPS-API-1  | GET /api/ops/carparks | 所有停车场及当前空闲车位数 |
| OPS-API-2  | GET /api/ops/users | 最近 30 秒内的不重复用户 |
| OPS 图片   | GET /api/ops/carparks/{carpark_id}/image | 运维仪表板使用的缓存标注图片 |
| 仪表板     | GET /dashboard/ | 运维仪表板 |
| probe      | GET /healthz、GET / | 存活状态 / 信息 |

OPS-REQ-1（请求日志）由中间件和 app/logging_utils.py 处理。
OPS-REQ-2（运维仪表板）可通过 `/dashboard/` 访问。页面由 FastAPI 提供，
浏览器使用 Plotly.js 绘图；车场遥测每 10 秒刷新，活跃用户每 5 秒刷新，
两个数据源会在运营依赖不可用时独立降级。

## 项目结构

    smartpark-api/
    ├─ app/
    │  ├─ main.py          # FastAPI 应用 + 所有路由 + 生命周期
    │  ├─ config.py        # 从 carparks.json 加载停车场（唯一事实来源）
    │  ├─ takephoto.py     # 摄像头服务的异步 HTTP 客户端
    │  ├─ inference.py     # YOLO 封装（运行时加载模型、线程池异步执行）
      │  ├─ cache.py         # 每个停车场的推理结果 TTL 缓存
      │  ├─ dashboard.py     # 运维仪表板路由
      │  ├─ static/          # 仪表板 HTML、CSS 和 Plotly.js 客户端代码
      │  └─ logging_utils.py # 结构化日志 + uuid 上下文 + 请求日志
    ├─ config/carparks.json  # 停车场列表（生成文件；会成为 GKE ConfigMap）
    ├─ scripts/make_carparks.py  # 重新生成 carparks.json
    ├─ requirements.txt
    ├─ Dockerfile          # 轻量化；模型权重不会打包进镜像
    ├─ .dockerignore / .gitignore
    └─ README.md

## 本地运行

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt   # 注意：也会安装 ultralytics
    .venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

如果模型不存在，应用会回退到 MockDetector，因此接口仍然可以运行（CPU 计数是确定性的替代值）。要使用真实 YOLO，请将 MODEL_PATH 设置为 model.pt / model.onnx 文件的路径。

## 配置（环境变量）

- CARPARKS_CONFIG  carparks.json 的路径          （默认 ./config/carparks.json）
- MODEL_PATH       模型权重的路径                 （默认 /models/model.pt）
- TAKEPHOTO_TIMEOUT  摄像头调用的超时时间（秒）    （默认 10）
- INFERENCE_WORKERS  YOLO 线程池大小              （默认 4）
- REQUEST_CACHE_TTL  每个停车场分析缓存的 TTL（秒）（默认 30）
- REQUEST_CACHE_REFRESH_AFTER  缓存达到该年龄后启动后台刷新
   （默认 20；必须大于 0 且小于 REQUEST_CACHE_TTL）
- REQUEST_CACHE_REFRESH_LOCK_TTL  Redis 刷新锁 TTL（秒，默认 30；应高于摄像头拉取与推理的预计总耗时）
- USER_ACTIVITY_STORE  共享用户活动后端：`redis` 或 `firestore`
   （默认 `firestore`；GKE 清单使用 `redis`）
- REDIS_URL       Redis 连接 URL（默认 `redis://redis-service:6379/0`）
- REDIS_TIMEOUT   Redis 连接/读取超时秒数（默认 `2`）
- FIRESTORE_DATABASE Firestore 数据库 ID（默认 `(default)`）
- PORT             监听端口                      （默认 8000）

## 关键设计决策

1. 停车场发现 = 唯一事实来源。平台启动时从 carparks.json 加载停车场。在 GKE 上，该 JSON 会作为 ConfigMap 挂载到每个副本中，因此只需编辑一个地方即可更新整个集群。
2. 可更新模型。模型权重**不会**复制到镜像中。在 GKE 上，initContainer 会将选定的 GCS 对象下载到共享的 `emptyDir` 卷中，应用通过 `MODEL_PATH=/data/model.pt` 读取该文件。修改 ConfigMap 中的模型 URI 并重启 Deployment 后，新 Pod 就会加载新模型，无需重新构建应用镜像。
3. 推理不会阻塞事件循环。YOLO 是同步且 CPU 密集型的，因此会通过 `loop.run_in_executor` 在 ThreadPoolExecutor 中运行；路由只需等待其结果（见 app/inference.py）。这是核心的“瓶颈 + 缓解措施”要点。
4. 2n 采样 + 并发执行。find-carparks 会采样 2*n 个停车场，并发拉取图片和执行推理（`asyncio.gather`），然后按空闲车位数返回前 n 个停车场。
5. 每个停车场的推理缓存。成功的分析结果（计数、置信度和带标注图片）会按停车场 ID 缓存指定 TTL，因此所有接口都可以复用该结果。OPS-API-1 会把成功缓存条目的 `created_at` 以 UTC ISO 8601 格式返回；运维图片接口直接复用缓存中的标注 PNG。选择 Redis 用户活动后端时，Redis 同时作为所有 Pod 共享的 L2 分析缓存：读取顺序为本地 L1、Redis L2、推理，推理成功后以相同 TTL 双写 L1 和 L2。L2 命中直接返回而不回填或续期 L1。后台刷新使用每个停车场一把带 token 的 Redis 锁协调；没有拿到锁的 Pod 直接返回当前缓存而不启动 YOLO，锁持有者在推理前再次检查 L2，避免其他 Pod 已完成刷新后重复推理。冷的 L1/L2 同时 miss 仍不加锁，偶尔可能并发推理。选择 Firestore 后端时仍只使用原有 L1 缓存。
6. 较大的 n（假设场景）。n 会被限制为停车场数量，最多只采样 2*n 个停车场，因此巨大的 n（例如 200）不会被滥用来请求摄像头或副本。
7. 共享的运营用户统计。当 `USER_ACTIVITY_STORE=redis` 时，OPS-API-2 使用共享 Redis ZSET，以用户为 member、最后访问时间为 score。GKE 清单运行一个集群内 Redis Pod。保留 Firestore 作为回退方案：设置 `USER_ACTIVITY_STORE=firestore` 即可切回；选定的存储不可用时接口返回 503，而不是报告误导性的单 Pod 统计数字。
8. 用户身份：uuid 优先，IP 作为回退。平台需要稳定的 ID，以便将同一用户的请求归组（用于日志和 OPS-API-2 用户统计）。如果客户端提供 uuid，就使用 `user:<uuid>`；否则回退到客户端 IP（`ip:<client_ip>`），这样一个用户发起多次请求（例如多次 annotate 调用）时只会计为一个用户，而不是多个用户。原始用户 uuid 仍会在 find-carparks 响应中原样返回。在代理 / 负载均衡器（GKE Ingress、Cloud Run）后方，我们从 X-Forwarded-For 读取真实客户端地址（取最左侧的值）；否则使用直接对端地址（见 app/logging_utils.py）。
   注意：IP 只是启发式身份标识，并不是真实身份：共享 NAT 会导致统计偏低，移动网络 IP 变化可能导致统计偏高，并且只应信任来自已知代理的 X-Forwarded-For。
9. 运维仪表板。`/dashboard/` 使用 `app/static/plotly-2.35.2.min.js` 本地 Plotly.js 文件。停车场每 10 秒刷新，活跃用户每 5 秒刷新。成功刷新显示 `Refreshed at`；之后刷新失败会保留上一次成功快照并标记 stale。点击可用停车场行会显示缓存标注图片，且不会影响活跃用户统计。

   缓存和 refresh lock 总结：
   - L1 是每个 Pod 的本地内存 `TTLCache`；只有当 `USER_ACTIVITY_STORE=redis` 时才启用共享 Redis L2。
   - 两级缓存都使用 `REQUEST_CACHE_TTL`（默认 30 秒）。L2 命中直接返回，不回填 L1，因此不会因为 L2 命中而延长原始缓存生命周期。
   - 缓存达到 `REQUEST_CACHE_REFRESH_AFTER`（默认 20 秒）后，当前请求立即返回仍有效的旧数据，同时尝试后台刷新。
   - Redis 模式使用 `REQUEST_CACHE_REFRESH_LOCK_TTL`（默认 30 秒）和每个停车场一把带 token 的锁，避免多个 Pod 重复后台刷新。Firestore 模式不会访问 Redis，仍保持原来的 L1-only 行为。

## 在 GKE 上统计缓存

API 会分别记录 L1 本地内存命中、L2 Redis 命中、未命中、后台刷新和写入事件。在 Linux shell 中运行以下命令，可以按 API Pod 独立统计：

```bash
for pod in $(kubectl get pods -l app=smartpark-api \
   -o jsonpath='{.items[*].metadata.name}'); do
   echo "===== $pod ====="

   kubectl logs "$pod" -c smartpark-api --tail=-1 |
      awk '
         /cache hit.*level=L1/ {
            l1_hit++
            if (/refresh=True/) l1_refresh++
            next
         }
         /cache hit.*level=L2/ {
            l2_hit++
            if (/refresh=True/) l2_refresh++
            next
         }
         /cache miss/                { miss++; next }
         /cache write.*level=L1/     { l1_write++; next }
         /cache write.*level=L2/     { l2_write++; next }
         /inference refresh started/ { refresh_started++; next }
         /inference refresh skipped.*reason=lock-held/ { lock_held++; next }
         /inference refresh skipped.*reason=l2-updated/ { l2_updated++; next }
         END {
            hits = l1_hit + l2_hit
            total = hits + miss
            printf "%-15s %d\n", "L1 hit", l1_hit + 0
            printf "%-15s %d\n", "L2 hit", l2_hit + 0
            printf "%-15s %d\n", "Miss", miss + 0
            printf "%-15s %d\n", "L1 refresh", l1_refresh + 0
            printf "%-15s %d\n", "L2 refresh", l2_refresh + 0
            printf "%-15s %d\n", "L1 write", l1_write + 0
            printf "%-15s %d\n", "L2 write", l2_write + 0
            printf "%-15s %d\n", "Refresh started", refresh_started + 0
            printf "%-15s %d\n", "Skipped lock-held", lock_held + 0
            printf "%-15s %d\n", "Skipped L2-updated", l2_updated + 0
            if (total > 0) {
               printf "%-15s %.2f%%\n", "L1 hit rate", 100 * l1_hit / total
               printf "%-15s %.2f%%\n", "L2 hit rate", 100 * l2_hit / total
               printf "%-15s %.2f%%\n", "Total hit rate", 100 * hits / total
            }
         }
      '
done
```

命中率的分母是 `L1 命中 + L2 命中 + miss`。缓存写入单独统计，不重复算作命中。`refresh=True` 表示当前请求会立即返回现有缓存，同时启动后台刷新。`kubectl logs` 读取当前容器保存的日志，因此比较不同 Pod 时应使用相同的压测时间窗口，并注意 Pod 重启会重置日志和 L1 缓存。

使用以下命令观察各 Pod 的 refresh lock 决策：

```bash
kubectl logs -l app=smartpark-api -c smartpark-api \
   --prefix --tail=-1 --max-log-requests=10 |
   grep -E 'inference refresh (started|skipped)'
```

`reason=lock-held` 表示另一个 Pod 正持有刷新锁；`reason=l2-updated` 表示当前 Pod 拿到锁后发现 L2 已被更新，因此跳过重复 YOLO。GKE 清单当前将锁 TTL 设置为 30 秒，约为已观察到的 15 秒 P95 的两倍；如果日志显示刷新经常超过锁的生命周期，应继续提高该值。

## OPS-API-1 响应语义

`/api/ops/carparks` 会为每个已配置停车场返回一行，包括失败的停车场：

- `success` / HTTP 200：全部停车场都可用。
- `partial` / HTTP 200：成功和不可用停车场混合。
- `error` / HTTP 503：所有已配置停车场都不可用。

不可用行仍保留停车场 ID 和名称，但 `available_spaces`、`confidence_score`、
`created_at` 返回 `null`。这样可以区分摄像头/推理失败和“当前空位为 0”。

## 后续步骤

- GKE 清单（命名空间、ConfigMap、节点服务账号 IAM 权限、Deployment、LoadBalancer Service、HPA）以及实际部署。模型使用 GCS + initContainer + 共享 `emptyDir`，而不是 PVC。

## 运行时模型部署

模型文件会被有意排除在 Docker 构建上下文和镜像之外。
请将模型上传到私有 GCS 存储桶，将 `MODEL_URI` 设置在
`k8s/model-configmap.yaml` 中，并授予 GKE 节点池服务账号
`storage.objects.get`（或 `roles/storage.objectViewer`）权限。Deployment 不会指定
`serviceAccountName`；Pod 使用命名空间 `default` 的 ServiceAccount，并通过当前集群配置中的节点身份访问 GCS。

### 部署集群内 Redis

Redis 清单运行一个不持久化的 Redis Pod，用于短期运营活动数据。所有 API 副本通过集群内部的 `redis-service:6379` 共享它。Redis 重启可能清空最近 30 秒的统计，但不会让核心 API 停止服务。

```bash
kubectl apply -f k8s/redis.yaml
kubectl rollout status deployment/smartpark-redis
```

API Deployment 默认设置 `USER_ACTIVITY_STORE=redis`。如需回退到 Firestore，将该变量删除或改为 `firestore`，然后重启 API Deployment。

### 生成停车场 ConfigMap

`carparks.json` 足够小，可以存储为 ConfigMap。源文件仍保留在仓库中，而下面的命令会根据该文件创建或更新 Kubernetes 资源：

```bash
kubectl create configmap smartpark-carparks \
   --from-file=carparks.json=config/carparks.json \
   --dry-run=client -o yaml | kubectl apply -f -
```

该 ConfigMap 由 `k8s/deployment.yaml` 挂载到
`/app/config/carparks.json`。应用会在启动时读取它，因此修改 `carparks.json` 后，请先应用 ConfigMap，再重启 Deployment：

```bash
kubectl rollout restart deployment/smartpark-api
kubectl rollout status deployment/smartpark-api
```

### 在 Compute Engine VM 上测试

VM 测试会有意将模型保留在镜像之外。VM 主机会先从 GCS 下载模型，然后 Docker 会将主机目录绑定挂载到应用容器中。容器通过 `MODEL_PATH` 读取 `/data/model.pt`。

```bash
mkdir -p ~/smartpark-models
gcloud storage cp \
   gs://YOUR_BUCKET/model.pt \
   ~/smartpark-models/model.pt
ls -lh ~/smartpark-models/model.pt
test -s ~/smartpark-models/model.pt && echo "model download succeeded"
```

即使该文件没有 Linux 执行权限，也可以通过 `bash` 执行对应的辅助脚本：

```bash
MODEL_URI=gs://YOUR_BUCKET/model.pt \
   bash scripts/download_model_vm.sh \
   ~/smartpark-models/model.pt
```

在 VM 上构建并运行镜像：

```bash
docker build -t smartpark-api:v1 .
docker run --rm \
   --name smartpark-api-test \
   -p 8000:8000 \
   -e FIRESTORE_DATABASE=fit3184-a1 \
   -e MODEL_PATH=/data/model.pt \
   -v ~/smartpark-models:/data:ro \
   smartpark-api:v1
```

`-v` 选项会让容器能够看到 VM 主机上的文件；它不会将模型复制到镜像中。在 VM 的第二个终端中，验证模型文件、健康检查接口和检测器模式：

```bash
docker exec smartpark-api-test ls -lh /data/model.pt
curl http://127.0.0.1:8000/healthz
docker logs smartpark-api-test 2>&1 | grep -Ei 'loading yolo|real yolo|mock|could not load|detector'
```

启动日志必须包含：

```text
Using REAL YOLO detector from /data/model.pt
detector=real-yolo
```

`detector=mock` 表示找不到模型文件或模型无法加载。仅凭 Base64 图片长度无法区分真实 YOLO 和 MockDetector；MockDetector 会返回原始图片，不会绘制检测框。

构建轻量镜像并推送到 Artifact Registry：

```bash
gcloud auth configure-docker REGION-docker.pkg.dev
docker build -t REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG .
docker push REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG
```

Kubernetes Deployment 会在镜像字段中替换 `REGION`、`PROJECT_ID` 和 `TAG`。initContainer 会将每个新 Pod 所需的一个模型下载到 `emptyDir` 中；因此修改 `MODEL_URI` 后需要执行 `kubectl rollout restart deployment/smartpark-api`。

创建 `k8s/deployment.yaml` 引用的停车场 ConfigMap，然后应用模型配置和 Deployment：

```bash
kubectl create configmap smartpark-carparks \
   --from-file=carparks.json=config/carparks.json \
   --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/model-configmap.yaml
kubectl apply -f k8s/deployment.yaml
```

- Locust 脚本 + 1/2/4/8 个副本的基准测试报告。
