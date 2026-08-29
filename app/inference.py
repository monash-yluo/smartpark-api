"""YOLO inference wrapper.
YOLO 推理封装.

This is the performance-critical piece, and the part the assignment explicitly
warns about:
  如果直接在异步路由里执行同步的 YOLO 预测,你会阻塞主事件循环,
  导致所有并发请求出现灾难性的延迟飙升.

YOLO's .predict() is synchronous and CPU-bound. We must NOT call it inline in a
coroutine. The pattern used here:
  * The heavy predict() runs on a dedicated ThreadPoolExecutor.
  * The FastAPI route is still async and simply awaits the executor
    (loop.run_in_executor), so the event loop stays free to serve other requests
    while this CPU-bound work runs on a worker thread.
YOLO 的 .predict() 是同步且 CPU 密集的.我们不能在协程里内联调用它.这里使用的模式:
  * 耗时的 predict() 在专用的 ThreadPoolExecutor 上运行.
  * FastAPI 路由仍是异步的,只需 await 执行器(loop.run_in_executor),
    因此当 CPU 密集工作在 worker 线程运行时,事件循环仍空闲以服务其他请求.

Why a thread pool (not a process pool)? YOLO/ultralytics releases the GIL during
the heavy NumPy / ONNX / torch kernel work, so threads give near-linear speedup
without the pickle overhead and memory duplication of a process pool. It is also
much easier to share the loaded model across workers.
为什么用线程池(而不是进程池)?YOLO/ultralytics 在 NumPy/ONNX/torch 内核运算期间会释放 GIL,
因此线程能带来接近线性的加速,且没有进程池的 pickle 开销和内存复制.跨 worker 共享已加载
模型也容易得多.

Model loading is intentionally NOT part of the image build: we load the weights
from MODEL_PATH at runtime (a mounted volume / PVC on GKE), so the model can be
updated without rebuilding the container. See build_detector() which falls back
to a MockDetector when no real model is present - useful for local dev/testing.
模型加载刻意不放入镜像构建:我们在运行时从 MODEL_PATH 加载权重(GKE 上的挂载卷 / PVC),
这样无需重建容器即可更新模型.见 build_detector(),当没有真实模型时会回退到 MockDetector,
便于本地开发/测试.
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
    """Runs the real YOLO model, keeping .predict() off the event loop.
    运行真实 YOLO 模型,让 .predict() 不占用事件循环."""

    def __init__(self, model_path: Path, workers: int = 4) -> None:
        self.model_path = Path(model_path)
        self.workers = max(1, workers)
        self.model = None
        # One thread pool shared by all inference calls. The number of workers is
        # the number of concurrent CPU-bound predictions this pod can run.
        # 所有推理调用共享一个线程池.worker 数量就是该 Pod 可同时运行的 CPU 密集预测数.
        self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="yolo")

    def load(self) -> None:
        """Load the model into memory. Import here so the module imports cleanly
        even when ultralytics is not installed (e.g. local dev without the ML stack).
        将模型加载到内存.在函数内 import,使得即使未安装 ultralytics(例如本地无 ML 环境)
        模块也能干净地导入."""
        from ultralytics import YOLO  # heavy import, keep it lazy 重导入,保持惰性

        log.info("Loading YOLO model from %s", self.model_path)
        self.model = YOLO(str(self.model_path))
        log.info("YOLO model loaded: names=%s", getattr(self.model, "names", None))

    async def analyze(self, image_bytes: bytes) -> dict:
        """Async facade: run predict on a worker thread and return a result dict.
        异步门面:在 worker 线程上运行 predict 并返回结果字典."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._analyze_sync, image_bytes)

    def _analyze_sync(self, image_bytes: bytes) -> dict:
        """Blocking detect + count. Return empty count, occupancy, confidence and
        an annotated PNG. One inference produces everything needed by both
        CORE-API-1 (count) and CORE-API-2 (image), so we never run the model
        twice for the same photo.
        阻塞式检测 + 计数.返回空位数量,占用数,置信度和标注 PNG.
        一次推理产出 CORE-API-1(计数)和 CORE-API-2(图片)所需的全部信息,
        因此对同一张照片我们从不会跑两次模型."""
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
        # r.plot() 返回一个已画好标注框的 BGR numpy 数组.
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
    bytes so that the "fetch top n" sorting logic is exercised.
    本地开发 / CI 中未安装模型或 ultralytics 时的确定性替身.通过对图片字节做哈希,
    生成伪随机但稳定的计数,从而演练"取前 n"的排序逻辑."""

    def __init__(self, workers: int = 4) -> None:
        self.workers = max(1, workers)

    async def analyze(self, image_bytes: bytes) -> dict:
        digest = hashlib.sha256(image_bytes).digest()
        empty = (digest[0] * 7) % 200 + 5
        occupied = (digest[1] * 5) % 100 + 1
        confidence = round(0.5 + (digest[2] / 255.0) / 2.0, 4)
        # For the mock, "annotated" equals the original image; enough to test the flow.
        # 对 Mock 而言,"标注图"等于原图;足以测试整个流程.
        return {
            "available_spaces": empty,
            "occupied_spaces": occupied,
            "confidence_score": confidence,
            "annotated_png": image_bytes,
        }


def build_detector(model_path: Path, workers: int = 4) -> Detector | MockDetector:
    """Return a real Detector if the model file exists and loads, else a MockDetector.
    如果模型文件存在且能加载则返回真实 Detector,否则返回 MockDetector.

    This is where the "updateable model" idea is honoured: the model is read from
    disk at runtime (not baked into the image). On GKE you mount the weights via a
    PVC (see k8s/pvc.yaml) and point MODEL_PATH at it; swapping the file under the
    mount updates the model without rebuilding the container.
    这正是"可更新模型"理念的体现:模型在运行时从磁盘读取(不烤进镜像).
    在 GKE 上你通过 PVC 挂载权重(见 k8s/pvc.yaml)并将 MODEL_PATH 指向它;
    只需替换挂载下的文件即可更新模型,无需重建容器.
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

    log.warning("USING MOCK DETECTOR - real YOLO not active. Set MODEL_PATH to enable.")
    return MockDetector(workers)
