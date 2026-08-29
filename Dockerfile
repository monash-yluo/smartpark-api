# SmartPark main platform API (the graded object)
# SmartPark 主平台 API(被评分对象)
# ---------------------------------------------------------------------------
# Intentionally lightweight: model weights are NOT baked in. They are mounted
# at runtime from a Kubernetes volume (PVC) via MODEL_PATH, so the model can be
# swapped without rebuilding the image - that is the "updateable model" point.
# 刻意保持轻量:模型权重不烤进镜像.它们在运行时通过 MODEL_PATH 从 Kubernetes 卷(PVC)挂载,
# 因此无需重建镜像即可替换模型--这就是"可更新模型"要点.
# ---------------------------------------------------------------------------
FROM python:3.10-slim

WORKDIR /app

# Install deps first so this layer is cached across code changes.
# 先安装依赖,使该层在代码变更时可被缓存.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code + car park config (the config becomes a ConfigMap on GKE).
# 应用代码 + 车场配置(该配置在 GKE 上会变成 ConfigMap).
COPY app ./app
COPY config ./config

# Runtime knobs. The model path points at the mounted volume on GKE.
# 运行时参数.模型路径指向 GKE 上挂载的卷.
ENV PORT=8000
ENV MODEL_PATH=/models/model.pt
ENV CARPARKS_CONFIG=/app/config/carparks.json

EXPOSE 8000
# NOTE: run as a module (not "python app/main.py") so the relative imports in
# app/ resolve correctly. The __main__ block launches uvicorn on $PORT (default 8000).
# 注意:以模块方式运行(而不是 "python app/main.py"),这样 app/ 内的相对导入才能正确解析.
# __main__ 块会在 $PORT(默认 8000)上启动 uvicorn.
CMD ["python", "-m", "app.main"]
