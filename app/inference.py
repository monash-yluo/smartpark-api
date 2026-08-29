"""YOLO inference wrapper.

This is the performance-critical piece, and the part the assignment explicitly
warns about:

  "If you simply execute the synchronous YOLO prediction inside a standard
   asynchronous FastAPI route, you will block the main event loop, causing
   catastrophic latency spikes for all concurrent requests."

YOLO's .predict() is synchronous and CPU-bound. We must NOT call it inline in a
coroutine. The pattern used here:

  * The heavy predict() runs on a dedicated ThreadPoolExecutor.
  * The FastAPI route is still async and simply awaits the executor
    (loop.run_in_executor), so the event loop stays free to serve other requests
    while this CPU-bound work runs on a worker thread.

Why a thread pool (not a process pool)? YOLO/ultralytics releases the GIL during
the heavy NumPy / ONNX / torch kernel work, so threads give near-linear speedup
without the pickle overhead and memory duplication of a process pool. It is also
much easier to share the loaded model across workers.

Model loading is intentionally NOT part of the image build: we load the weights
from MODEL_PATH at runtime (a mounted volume / PVC on GKE), so the model can be
updated without rebuilding the container. See build_detector() which falls back
to a MockDetector when no real model is present — useful for local dev/testing.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

log = logging.getLogger("smartpark.inference")


class Detector:
    """Runs the real YOLO model, keeping .predict() off the event loop."""

    def __init__(self, model_path: Path, workers: int = 4) -> None:
        self.model_path = Path(model_path)
        self.workers = max(1, workers)
        self.model = None
        # One thread pool shared by all inference calls. The number of workers is
        # the number of concurrent CPU-bound predictions this pod can run.
        self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="yolo")

    def load(self) -> None:
        """Load the model into memory. Import here so the module imports cleanly
        even when ultralytics is not installed (e.g. local dev without the ML stack)."""
        from ultralytics import YOLO  # heavy import, keep it lazy

        log.info("Loading YOLO model from %s", self.model_path)
        self.model = YOLO(str(self.model_path))
        log.info("YOLO model loaded: names=%s", getattr(self.model, "names", None))

    async def analyze(self, image_bytes: bytes) -> dict:
        """Async facade: run predict on a worker thread and return a result dict."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._analyze_sync, image_bytes)

    def _analyze_sync(self, image_bytes: bytes) -> dict:
        """Blocking detect + count. Return empty count, occupancy, confidence and
        an annotated PNG. One inference produces everything needed by both
        CORE-API-1 (count) and CORE-API-2 (image), so we never run the model
        twice for the same photo."""
        if self.model is None:
            raise RuntimeError("Detector.load() was not called")

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        results = self.model.predict(img, verbose=False)
        r = results[0]

        empty_count = 0
        occupied_count = 0
        confidences: list[float] = []

        for box in r.boxes:
            label = r.names[int(box.cls[0].item())]
            conf = float(box.conf[0].item())
            confidences.append(conf)
            if label == "empty":
                empty_count += 1
            elif label == "occupied":
                occupied_count += 1

        confidence = sum(confidences) / len(confidences) if confidences else 0.0

        # r.plot() returns a BGR numpy array with the annotated boxes drawn.
        annotated_bgr = r.plot()
        annotated_rgb = annotated_bgr[:, :, ::-1]
        buf = io.BytesIO()
        Image.fromarray(annotated_rgb).save(buf, format="PNG")

        return {
            "available_spaces": empty_count,
            "occupied_spaces": occupied_count,
            "confidence_score": confidence,
            "annotated_png": buf.getvalue(),
        }


class MockDetector:
    """Deterministic stand-in for local dev / CI when no model or ultralytics is
    installed. Produces pseudo-random-but-stable counts from a hash of the image
    bytes so that the "fetch top n" sorting logic is exercised."""
    def __init__(self, workers: int = 4) -> None:
        self.workers = max(1, workers)

    async def analyze(self, image_bytes: bytes) -> dict:
        digest = hashlib.sha256(image_bytes).digest()
        empty = (digest[0] * 7) % 200 + 5
        occupied = (digest[1] * 5) % 100 + 1
        confidence = round(0.5 + (digest[2] / 255.0) / 2.0, 4)
        # For the mock, "annotated" equals the original image; enough to test the flow.
        return {
            "available_spaces": empty,
            "occupied_spaces": occupied,
            "confidence_score": confidence,
            "annotated_png": image_bytes,
        }


def build_detector(model_path: Path, workers: int = 4) -> Detector | MockDetector:
    """Return a real Detector if the model file exists and loads, else a MockDetector.

    This is where the "updateable model" idea is honoured: the model is read from
    disk at runtime (not baked into the image). On GKE you mount the weights via a
    PVC (see k8s/pvc.yaml) and point MODEL_PATH at it; swapping the file under the
    mount updates the model without rebuilding the container.
    """
    if model_path and Path(model_path).is_file():
        try:
            det = Detector(model_path, workers)
            det.load()
            log.info("Using REAL YOLO detector from %s", model_path)
            return det
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load model %s (%s); falling back to MOCK.", model_path, exc)
    else:
        log.warning("Model file not found at %s; falling back to MOCK.", model_path)

    log.warning("USING MOCK DETECTOR — real YOLO not active. Set MODEL_PATH to enable.")
    return MockDetector(workers)
