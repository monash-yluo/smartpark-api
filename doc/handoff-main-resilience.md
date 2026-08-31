# Handoff: `/api/find-carparks` 韧性改进

> 供后续 AI 会话/开发者快速接手的交接文档。
> 对象文件:`app/main.py`
> 日期:2026-08-30

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

## 现在的响应语义(三级)

| 场景 | HTTP 状态 | 说明 |
|------|-----------|------|
| 单个/部分车场失败 | 200 | 返回成功的那部分,`failed_carparks` 披露失败数 |
| 全部车场失败 | 503 | `msg: all sampled car parks are unavailable` |
| 全部成功 | 200 | `failed_carparks: 0`,正常返回 top n |

## 影响范围

- `_analyze_carpark` 同时被 `find_carparks` 和 `ops_carparks` 复用,因此
  `OPS-API-1 /api/ops/carparks` 也自动获得了「单个车场推理失败被跳过」的行为。
- 本次没有为 `ops_carparks` 增加「全部失败返回 503」逻辑(保持现状:返回
  `count: 0, carparks: []`)。如需一致,可在该端点加同样判断。

## 待办/可优化

- [ ] 决定是否给 `ops_carparks` 也加「全部失败返回 503」。
- [ ] 单个车场的拉图或推理失败时不会写入按停车场 ID 的分析缓存,
      因此之后的请求会重试;这是避免缓存暂时性故障的预期行为。

## 验证

已通过 `get_errors` 检查:`app/main.py` 无语法/静态错误。

## 需要用到的上下文(其他 AI 会话)

- 模型加载在 `app/inference.py`,`Detector._analyze_sync` 里 `Image.open` /
  `model.predict` 可能抛异常;`MockDetector.analyze` 不会。
- 拉图在 `app/takephoto.py`,`fetch_image` 抛 `TakephotoError`。
- RESTful 语义:单点故障降级(200 + 部分结果)是**有意为之的韧性设计**,
  不应为了个别失败返回 4xx/5xx。
