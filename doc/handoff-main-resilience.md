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

## 文档同步: ConfigMap 与 VM Docker 验证

> 更新日期:2026-09-04。

本次将完整操作流程同步到 `README.md`，包括以下内容:

- `config/carparks.json` 通过 `kubectl create configmap ... --from-file` 生成或更新
    `smartpark-carparks` ConfigMap；该 ConfigMap 在 GKE 中挂载到
    `/app/config/carparks.json`。
- 修改车场配置后执行 `kubectl apply`（或重新生成 ConfigMap）以及
    `kubectl rollout restart deployment/smartpark-api`，因为应用只在启动时加载配置，
    不承诺实时热更新。
- Compute Engine VM 上使用 `gcloud storage cp` 将
    `gs://YOUR_BUCKET/model.pt` 下载到 `~/smartpark-models/model.pt`。
- VM 上构建镜像:

    ```bash
    docker build -t smartpark-api:v1 .
    ```

- VM 上运行容器时使用:

    ```bash
    docker run --rm --name smartpark-api-test -p 8000:8000 \
        -e MODEL_PATH=/data/model.pt \
        -v ~/smartpark-models:/data:ro \
        smartpark-api:v1
    ```

    模型留在 VM 主机上，通过 Docker bind mount 进入 `/data/model.pt`，没有被复制进镜像。
- 在另一个 VM 终端通过 `docker exec`、`curl /healthz` 和 `docker logs` 验证模型文件、服务
    状态与 detector 模式。真实模型应显示 `Using REAL YOLO detector` 和
    `detector=real-yolo`；`detector=mock` 表示模型不存在或加载失败。
- `scripts/download_model_vm.sh` 没有执行权限时，可以使用
    `bash scripts/download_model_vm.sh ...`，不必修改文件权限。
- 文档明确说明 Base64 图片很长不能证明使用真实模型；MockDetector 会原样返回输入图片，
    应以启动日志中的 detector 模式为准。
