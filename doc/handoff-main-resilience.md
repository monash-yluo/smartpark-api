# Handoff: API 韧性、按停车场缓存与 Single-Flight

> 供后续 AI 会话/开发者快速接手的交接文档。
> 对象文件:`app/main.py`
> 更新日期:2026-09-02

## 背景

`app/main.py` 是 SmartPark 主平台 API(单体服务,GKE 上水平扩展)。核心端点
`CORE-API-1` `GET /api/find-carparks?uuid=...&n=3` 会在随机抽样的一批车场里,
并发「拉图 + YOLO 推理」,然后返回空位最多的前 n 个车场。

关键函数 `_analyze_carpark(carpark)` 原本**只对「拉图」做了异常保护**,对
「推理」没有,导致单个车场在推理阶段报错时会拖垮整个请求(返回 500)。

## 本次改动

### 1. `_analyze_carpark`:推理阶段也做异常保护(方案一)

之前:

```python
    except TakephotoError as exc:
        log.warning("carpark %s camera error: %s", carpark.id, exc)
        return None
    analysis = await app.state.detector.analyze(image)
```

现在(为 `analyze` 加了 try/except,出错返回 `None`):

```python
    except TakephotoError as exc:
        log.warning("carpark %s camera error: %s", carpark.id, exc)
        return None
    try:
        analysis = await app.state.detector.analyze(image)
    except Exception as exc:  # noqa: BLE001 - a single bad photo must not fail the whole request
        log.warning("carpark %s analyze error: %s", carpark.id, exc)
        return None
```

**效果**:拉图失败(`TakephotoError`)和推理失败都返回 `None`。
`asyncio.gather` 收集到的 `None` 会被 `ok = [r for r in results if r is not None]`
过滤掉,单个车场故障不再影响整个请求。

### 2. `find_carparks`:全部失败返回 503

在 `ok` 计算后新增「全部失败」判断:

```python
    results = await asyncio.gather(*(_analyze_carpark(cp) for cp in chosen))
    ok = [r for r in results if r is not None]
    # If every sampled car park failed (camera down / inference error), we cannot
    # produce any results. Return 503 instead of pretending there are simply no
    # free spaces (which would be misleading to callers).
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
```

**效果**:避免「全部摄像头/推理宕机却假装 results 为空」的误导,更符合 RESTful 语义。

### 3. `find_carparks`:成功响应披露失败数

成功 payload 新增 `failed_carparks` 字段:

```python
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
```

### 4. 缓存从按用户响应改为按停车场分析结果

原先 `find_carparks` 使用 `(user_id, eff_n)` 缓存整个列表响应。这个设计不能让
`annotate-carpark` 复用推理结果,也可能让不同时间的随机采样结果在 TTL 内保持不变。

现在使用命名空间停车场键:

```python
cache_key = ("carpark-analysis", carpark.id)
```

缓存值是完整的 `analysis`:

```python
{
    "available_spaces": ...,
    "occupied_spaces": ...,
    "confidence_score": ...,
    "annotated_png": ...,
}
```

新增 `_get_carpark_analysis(carpark)` 作为共享入口:

- 命中 TTL 缓存时直接返回完整分析,不重新拉图或运行模型。
- 未命中时由 `_load_and_cache_carpark_analysis` 拉图并推理。
- 只有成功结果写入缓存;相机或推理失败不会缓存,后续请求可以重试。
- `find-carparks`、`annotate-carpark` 和 `ops/carparks` 都通过这条路径取得结果。
- `user_id` 仍用于日志和 OPS-API-2 用户统计,不再参与推理缓存。

`REQUEST_CACHE_TTL` 现在表示按停车场分析缓存的 TTL,默认 30 秒。TTL 内返回的是同一
次拍照和推理结果,因此可能有最多一个 TTL 窗口的实时性延迟。

### 5. 同一停车场的 single-flight

仅使用 TTL 缓存仍存在 cache stampede:两个请求可以同时发现缓存未命中,然后各自创建
推理任务。启动时现在初始化:

```python
app.state.inflight_analyses = {}
app.state.inflight_analyses_lock = asyncio.Lock()
```

`_get_carpark_analysis` 在锁内完成“二次查缓存、查找进行中 Task、必要时创建并登记
Task”这组操作。相同 `carpark.id` 的并发请求等待同一个 Task,不同停车场仍可并发处理。

等待使用:

```python
await asyncio.shield(task)
```

这样某一个 HTTP 请求取消时,只取消自己的等待,不会取消仍可能服务其他请求的共享拉图/
推理任务。Task 通过 `add_done_callback` 自己清理 in-flight 表;清理前检查字典中的
Task 身份,避免旧任务误删同一停车场的新任务。

共享的是完整 `analysis`,不是 HTTP response。两个 API 仍分别构造自己的响应:
`find-carparks` 提取计数,`annotate-carpark` 将 `annotated_png` 编码为 Base64。

### 6. 测试与文档同步

`tests/test_find_carparks_resilience.py` 新增 single-flight 测试:

- 同时请求两个不同停车场,每个停车场各发起两次并发分析。
- 计数 detector 作为 YOLO `detector.analyze()` 的测试替身。
- 断言总共只有 2 次相机获取和 2 次推理,即每个停车场一个 Task。
- 任务完成后再次请求两个停车场,断言调用次数不增加,证明使用完成缓存。
- 断言 in-flight 表在任务结束后为空。

`app/cache.py` 和本文件的缓存说明已同步为按停车场分析缓存。

### 7. 请求取消不会误取消共享分析 Task

调用方等待共享 Task 时使用:

```python
return await asyncio.shield(task)
```

`shield()` 让 HTTP 请求取消只影响该请求自己的等待，不会向 `task` 传播取消。
因此另一位正在等待同一车场分析的调用方仍能获得结果，成功结果也仍会写入缓存。

测试使用可控的阻塞 detector 验证：取消第一个等待者后，第二个等待者继续获得结果；
推理只运行一次，缓存成功写入，in-flight 表最终清空。

### 8. 优雅停机收尾 in-flight Task

服务 lifespan 退出时现在会调用 `_shutdown_inflight_analyses(app)`：

1. 在 `inflight_analyses_lock` 内快照并清空已登记 Task；
2. 对尚未完成的 Task 调用 `cancel()`；
3. `await asyncio.gather(*tasks, return_exceptions=True)`，等待 Task 实际结束；
4. 最后才执行 `await app.state.http.aclose()`。

`task.cancel()` 只是发出取消请求，Task 要在后续可取消的 `await` 点处理
`CancelledError` 并完成 finally 清理；因此必须 `gather()` 等待。测试验证 Task 的
清理事件发生在 HTTP client 关闭之前。

### 9. Refresh-ahead 缓存

为减少 TTL 临界点的等待延迟，缓存层保留旧的严格 `TTLCache.get()` 接口，并新增:

```python
lookup = cache.get_with_refresh(key, refresh_after)
```

该方法只返回尚未过期的缓存值，并通过 `lookup.should_refresh` 标记是否已进入提前刷新
窗口。当前默认配置为:

```text
REQUEST_CACHE_TTL=30
REQUEST_CACHE_REFRESH_AFTER=20
```

约束为 $0 < refresh_after < TTL$。请求行为如下：

| 缓存年龄 | 行为 |
|-----------|------|
| 0-20 秒 | 立即返回缓存，不刷新 |
| 20-30 秒 | 立即返回缓存，同时通过 `_get_or_start_analysis_task()` 去重地启动后台刷新 |
| 大于 30 秒 | 严格 miss；等待已有共享 Task 或创建一个并等待其结果 |

后台刷新 Task 和普通 cache miss 共用 `inflight_analyses`，故同一 Pod 的同一车场最多只有
一个拉图/推理任务；它也会被优雅停机逻辑统一取消和等待。调用 `_get_or_start_analysis_task()`
时需要 `await`，但只等待短暂的 async lock、查找与 Task 登记，**不等待**后台拉图或推理；
真正耗时工作由 `asyncio.create_task()` 调度。

新增测试验证：刷新窗口内的两个并发调用立刻取得旧缓存，只运行一次后台推理，后台完成后
缓存替换为新结果。

## 现在的响应语义(三级)

| 场景 | HTTP 状态 | 说明 |
|------|-----------|------|
| 单个/部分车场失败 | 200 | 返回成功的那部分,`failed_carparks` 披露失败数 |
| 全部车场失败 | 503 | `msg: all sampled car parks are unavailable` |
| 全部成功 | 200 | `failed_carparks: 0`,正常返回 top n |

## 影响范围

- `_analyze_carpark` 同时被 `find_carparks` 和 `ops_carparks` 复用,因此
  `OPS-API-1 /api/ops/carparks` 也自动获得了「单个车场推理失败被跳过」的行为。
- `_analyze_carpark` 现在只负责把完整分析转换为列表摘要;
    `_get_carpark_analysis` 负责缓存和 single-flight;
    `_load_and_cache_carpark_analysis` 负责实际拉图、推理和成功缓存。
- 本次没有为 `ops_carparks` 增加「全部失败返回 503」逻辑(保持现状:返回
  `count: 0, carparks: []`)。如需一致,可在该端点加同样判断。
- 缓存和 in-flight 表都是单进程/单 Pod 内存状态。不同 GKE 副本之间不能互相复用。

## 待办/可优化

- [x] 为 `_get_carpark_analysis` 增加请求取消测试：等待者取消不会取消共享 Task。
- [x] 在 lifespan 退出时取消并 await in-flight Task，再关闭 HTTP client。
- [x] 加入 refresh-ahead：20 秒后后台刷新、30 秒严格 TTL 失效。
- [ ] **优先级高:** 为关闭期间阻止新 Task 创建增加 `app.state.shutting_down` 状态。
    当前 shutdown 清空 in-flight 表后，理论上仍可能有一个已在运行的请求随后进入
    `_get_or_start_analysis_task()` 并创建新 Task；该 Task 不在 shutdown 快照中，可能与
    已关闭的 HTTP client 竞争。应在 lifespan 退出开始时置位，在创建 Task 前检查并拒绝新任务。
- [ ] **优先级中:** 给 refresh-ahead 加入失败后的重试节流/退避。当前刷新失败不会覆盖旧缓存，
    这是正确的；但在 20-30 秒窗口内，每次后续命中都可能再次触发一次刷新，故障相机可能产生
    额外请求。
- [ ] **优先级中:** 若需要跨副本去重,使用 Redis 等共享组件实现分布式锁/结果缓存;
    当前 `asyncio.Lock` 只在单个进程内有效。
- [ ] **优先级中:** 给按停车场缓存增加容量上限或定期清理策略。当前 TTLCache 只在
    读取过期键时惰性删除,大量停车场 ID 变化时可能积累过期条目。
- [ ] **优先级中:** 评估缓存的“按停车场 ID”粒度与实时性要求。如果必须识别画面变化,
    可每次拉图后按图片哈希缓存推理结果,但这样不能省掉相机请求。
- [ ] **优先级低:** 可以给缓存命中/未命中、single-flight 等待、推理耗时增加指标,
    便于 Locust 和 GKE 压测判断优化是否有效。
- [ ] **优先级低:** `TTLCache` 使用 `time.time()` 计算年龄，系统时间回拨或跳跃可能使
    TTL/刷新窗口不准确；若需要更稳健的本地计时，可改为 `time.monotonic()`。
- [ ] `ops_carparks` 是否在全部停车场失败时返回 503 仍需按作业语义决定;当前保持原行为。
- [ ] 单个车场的拉图或推理失败时不会写入按停车场 ID 的分析缓存,
    因此之后的请求会重试;这是避免缓存暂时性故障的预期行为。
- [ ] 文档同步：`README.md` 的环境变量列表尚未列出
    `REQUEST_CACHE_REFRESH_AFTER`，应补上默认值 `20` 与约束。

## 验证

已通过 `get_errors` 检查:`app/main.py`、`app/cache.py` 和测试文件无错误。

运行命令:

```powershell
.venv\Scripts\python.exe tests\test_find_carparks_resilience.py
```

最终结果:**13/13 passed**。测试覆盖 UUID 校验、全部相机失败、单个/全部推理失败、
部分失败、全成功、同一停车场 single-flight、不同停车场并发隔离、缓存复用、请求取消、
优雅停机以及 refresh-ahead 后台刷新。

## 需要用到的上下文(其他 AI 会话)

- 模型加载在 `app/inference.py`,`Detector._analyze_sync` 里 `Image.open` /
  `model.predict` 可能抛异常;`MockDetector.analyze` 不会。
- 拉图在 `app/takephoto.py`,`fetch_image` 抛 `TakephotoError`。
- `TTLCache` 使用 `threading.Lock` 保护底层字典;它与用于协调 in-flight Task 的
    `asyncio.Lock` 是两个不同层次的锁。
- RESTful 语义:单点故障降级(200 + 部分结果)是**有意为之的韧性设计**,
  不应为了个别失败返回 4xx/5xx。

## 模型运行时部署: GCS + initContainer + emptyDir

> 更新日期:2026-09-04。此节是已讨论并落地的 Updateable Model 方案。

### 作业要求与方案决定

作业 §4.3 的要求是:容器镜像保持轻量，模型权重在运行时动态取得/挂载。
没有要求训练、微调、自动检测模型变更或应用内热更新。

最终采用 **GCS + initContainer + emptyDir**:

```text
GCS bucket
    -> initContainer: gsutil cp $MODEL_URI /data/model.pt
    -> Pod 共享 emptyDir(model-data)
    -> FastAPI container: MODEL_PATH=/data/model.pt
```

- `model.pt` 约 44 MB，超过 ConfigMap 约 1 MB 的容量限制，存放在私有 GCS bucket。
- `carparks.json` 很小，继续由 ConfigMap 提供。
- initContainer 在每个新 Pod 启动时运行一次并下载模型；主容器只在模型下载成功后启动。
- `emptyDir` 仅在该 Pod 的生命周期内保留数据。Pod 删除或重建后模型会消失，新 Pod 会重新下载。
- 更新模型的方式是上传新对象、修改 `MODEL_URI`、再 rollout restart；**不实现热更新**。
- 未选择 gcsfuse，因为它需要启用 GKE CSI addon 与额外配置；对本作业并非必要。PVC 也不是本方案的一部分。

### 已更改文件

1. `Dockerfile`

     - 默认 `MODEL_PATH` 从 `/models/model.pt` 改为 `/data/model.pt`。
     - Dockerfile 不包含 GCS 下载逻辑；initContainer 是 Kubernetes Pod 层的配置，不能写进 Dockerfile。
     - 镜像仍可正常 build/push 到 Artifact Registry，且不包含模型权重。

2. `.dockerignore`

     新增以下排除规则，防止未来将模型意外打进 Docker build context:

     ```text
     models/
     *.pt
     *.onnx
     ```

3. `k8s/model-configmap.yaml`（新增）

     ```yaml
     apiVersion: v1
     kind: ConfigMap
     metadata:
         name: smartpark-model-config
     data:
         MODEL_URI: gs://REPLACE_WITH_BUCKET/model-v1.pt
     ```

     部署前必须替换为真实的 `gs://bucket/object.pt`。ConfigMap 注入 env 后是 Pod
     启动快照，所以改 ConfigMap 后必须重启 Deployment。

4. `k8s/deployment.yaml`（新增）

     - `model-data` 是一个 `emptyDir` volume。
     - `download-model` initContainer 使用
         `gcr.io/google.com/cloudsdktool/google-cloud-cli:slim`。
     - 它从 `smartpark-model-config` 读取 `MODEL_URI`，运行:

         ```sh
         gsutil cp "${MODEL_URI}" /data/model.pt
         test -s /data/model.pt
         ```

     - 主容器将相同 volume 只读挂载到 `/data`，通过 `MODEL_PATH=/data/model.pt`
         加载真实 YOLO。
     - `carparks-config` 期望名为 `smartpark-carparks` 的 ConfigMap，并以
         `subPath: carparks.json` 挂载至 `/app/config/carparks.json`。
     - `image` 仍是占位符，部署前替换为 Artifact Registry 地址。
     - Deployment **没有** `serviceAccountName`。Pod 使用 namespace 的 `default`
         Kubernetes ServiceAccount。

5. `scripts/download_model_vm.sh`（新增）

     用于在 Linux Compute Engine VM 主机下载模型，模拟 GKE initContainer 的 GCS 下载操作:

     ```bash
     MODEL_URI=gs://bucket/model-v1.pt \
         ./scripts/download_model_vm.sh ~/smartpark-models/model.pt
     ```

     脚本会创建父目录、执行 `gsutil cp`、检查目标文件非空、并输出下载大小。

6. `README.md`、`app/main.py`、`app/inference.py`

     - 文档和源码注释从 PVC 方案改为 GCS + initContainer + emptyDir。
     - 应用逻辑没有改动: `app/config.py` 从 `MODEL_PATH` 读路径；
         `build_detector()` 使用 `Path(model_path).is_file()`，文件存在则加载真 YOLO，
         否则回退至 `MockDetector`。

### VM 上验证 Docker 镜像的原理

Docker 镜像本身不会自动从 GCS 下载模型。VM 上的验证流程是:

```text
VM host 的 gsutil
    -> 使用 VM Compute Engine service account 从 GCS 下载模型
    -> VM host 文件: ~/smartpark-models/model.pt
    -> Docker bind mount: -v ~/smartpark-models:/data:ro
    -> app container 从 /data/model.pt 读取模型
```

运行容器的示例:

```bash
docker run --rm --name smartpark-api-test -p 8000:8000 \
    -e MODEL_PATH=/data/model.pt \
    -v ~/smartpark-models:/data:ro \
    REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG
```

日志应出现 `Using REAL YOLO detector from /data/model.pt`，而不是
`USING MOCK DETECTOR`。这证明模型没有烤进镜像，却可以在运行时通过 volume
提供给应用。

### 当前 GCP 身份与权限决定

用户此前已验证 GKE 节点可使用默认 Compute Engine 服务账号拉取 Artifact Registry 镜像，
当前集群预计使用:

```text
PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

因此当前方案不建立 `smartpark-api` Kubernetes ServiceAccount，也不配置 Workload
Identity。应将模型 GCS bucket 的只读权限授予 **GKE 节点池实际使用的 Google
Service Account**。先确认节点池身份:

```bash
gcloud container node-pools describe NODE_POOL_NAME \
    --cluster CLUSTER_NAME \
    --location ZONE_OR_REGION \
    --format="value(config.serviceAccount)"
```

然后授权最小所需的 bucket 权限:

```bash
gcloud storage buckets add-iam-policy-binding gs://MODEL_BUCKET \
    --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectViewer"
```

`roles/artifactregistry.reader` 仅用于节点拉取镜像；GCS 下载模型另需
`roles/storage.objectViewer`（或等效的 `storage.objects.get`）。VM 默认 service
account 的权限只证明 VM 能访问 GCS；GKE 部署前仍要确认 node pool 确实使用同一账号。

不要用 `gcloud auth application-default login` 验证 VM service account 权限，
因为它可能改为使用个人 Google 账号。可在 VM 上查询 metadata service account:

```bash
curl -H "Metadata-Flavor: Google" \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email
```

### 部署与验证命令

1. 构建及推送镜像:

     ```bash
     gcloud auth configure-docker REGION-docker.pkg.dev
     docker build -t REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG .
     docker push REGION-docker.pkg.dev/PROJECT_ID/smartpark/api:TAG
     ```

     推送身份需要 Artifact Registry Writer；GKE 节点只需 Reader 来拉取。

2. 在 `k8s/deployment.yaml` 替换 image 地址，并在
     `k8s/model-configmap.yaml` 填入真实 `MODEL_URI`。

3. 创建/更新 carpark ConfigMap 并应用模型相关资源:

     ```bash
     kubectl create configmap smartpark-carparks \
         --from-file=carparks.json=config/carparks.json \
         --dry-run=client -o yaml | kubectl apply -f -
     kubectl apply -f k8s/model-configmap.yaml
     kubectl apply -f k8s/deployment.yaml
     kubectl rollout status deployment/smartpark-api
     ```

4. 验证 initContainer 与真实模型加载:

     ```bash
     kubectl get pods -l app=smartpark-api
     kubectl logs POD_NAME -c download-model
     kubectl logs POD_NAME -c smartpark-api
     ```

     initContainer 日志应显示 `Copying gs://...`；主容器日志应显示真实 YOLO
     detector。失败时优先执行 `kubectl describe pod POD_NAME`，检查
     `Init:Error`、GCS IAM 和 `MODEL_URI`。

5. 更新模型:

     ```bash
     kubectl apply -f k8s/model-configmap.yaml
     kubectl rollout restart deployment/smartpark-api
     kubectl rollout status deployment/smartpark-api
     ```

### 验证状态与环境限制

- 静态诊断（`get_errors`）已检查 `Dockerfile`、Deployment、ConfigMap、脚本、README、
    `app/main.py`、`app/inference.py`，均无错误。
- 当前 Windows 工作区环境未安装 `docker` 和 `kubectl`，因此这里尚未执行镜像 build、
    Docker 运行、`kubectl apply --dry-run` 或实际 GCS 下载。
- 已检查仓库中不存在 `.pt` 或 `.onnx` 模型文件。
