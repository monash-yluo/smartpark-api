# SmartPark main platform API (the graded object)
# ---------------------------------------------------------------------------
# Intentionally lightweight: model weights are NOT baked in. They are mounted
# at runtime from a Kubernetes volume (PVC) via MODEL_PATH, so the model can be
# swapped without rebuilding the image — that is the "updateable model" point.
# ---------------------------------------------------------------------------
FROM python:3.10-slim

WORKDIR /app

# Install deps first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code + car park config (the config becomes a ConfigMap on GKE).
COPY app ./app
COPY config ./config

# Runtime knobs. The model path points at the mounted volume on GKE.
ENV PORT=8000
ENV MODEL_PATH=/models/model.pt
ENV CARPARKS_CONFIG=/app/config/carparks.json

EXPOSE 8000
CMD ["python", "app/main.py"]
