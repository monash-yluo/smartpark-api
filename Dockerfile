# SmartPark main platform API (the graded object)
# SmartPark 主平台 API(被评分对象)
# ---------------------------------------------------------------------------
# Base: ultralytics' official CPU image. torch-cpu + ultralytics + OpenCV are
# already installed and working (headless, no GUI/Qt), so we avoid the
# libGL/libglib crash that puts a minimal Python image + the FULL `ultralytics`
# package (which pulls the GUI build of opencv-python) at risk, and we skip
# re-downloading torch on every build.
# 基础镜像:使用 ultralytics 官方 CPU 镜像.其中已内置并可用 torch-cpu +
# ultralytics + OpenCV(headless, 无 GUI/Qt),因此避免了精简 Python 镜像 +
# 完整版 ultralytics(会拉 GUI 版 opencv-python)易踩到的 libGL/libglib 崩溃,
# 也省去每次 build 重下 torch.
#
# Pinned (NOT "latest") so the build is reproducible, and to match the ultralytics
# version tested locally (8.4.133). See requirements.txt (ultralytics is NOT listed
# there because the base image already provides it).
# 固定版本(而非 latest)以保证可复现,并与本地实测的 ultralytics 版本(8.4.133)一致.
# requirements.txt 里不再列出 ultralytics,因为基础镜像已提供.
# ---------------------------------------------------------------------------
FROM ultralytics/ultralytics:8.4.133-cpu

WORKDIR /app

# Install our API deps first (fastapi/uvicorn/httpx/pillow), which are NOT in the
# base image, so this layer is cached across code changes.
# 先安装我们的 API 依赖(fastapi/uvicorn/httpx/pillow)——这些不在基础镜像里,
# 使该层在代码变更时能被缓存.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code + car park config (the config becomes a ConfigMap on GKE).
# 应用代码 + 车场配置(该配置在 GKE 上会变成 ConfigMap).
COPY app ./app
COPY config ./config

# Runtime knobs. The model path points at the mounted volume (PVC) on GKE, so the
# model is NOT baked into the image (the "updateable model" point).
# 运行时参数.模型路径指向 GKE 上挂载的卷(PVC),所以模型不烤进镜像("可更新模型"要点).
ENV PORT=8000
ENV MODEL_PATH=/models/model.pt
ENV CARPARKS_CONFIG=/app/config/carparks.json

EXPOSE 8000
# NOTE: run as a module (NOT "python app/main.py") so the relative imports in
# app/ resolve correctly. The __main__ block launches uvicorn on $PORT.
# 注意:以模块方式运行(而不是 "python app/main.py"),这样 app/ 内的相对导入才能正确解析.
CMD ["python", "-m", "app.main"]
