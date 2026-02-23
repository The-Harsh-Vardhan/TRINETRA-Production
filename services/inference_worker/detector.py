"""
YOLOv8 Detector — TRINETRA Inference Worker
--------------------------------------------
Wraps a TensorRT-compiled YOLOv8 engine for person detection.
Supports batch inference with configurable input size.

TensorRT provider selection:
  - Primary: TensorRT Execution Provider (via ONNX Runtime or direct TRT API)
  - Fallback: CUDA Execution Provider
  - Last resort: CPU Execution Provider

For production: use ultralytics' native TensorRT export and inference
to get access to NMS baked into the engine graph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import cv2

logger = logging.getLogger("inference_worker.detector")


@dataclass
class Detection:
    track_id: int = 0               # Assigned by ByteTrack AFTER detection
    bbox: list = None               # [x1, y1, x2, y2] normalized
    confidence: float = 0.0
    class_id: int = 0               # 0 = person (COCO)

    def __post_init__(self):
        if self.bbox is None:
            self.bbox = []


class YOLOv8Detector:
    """
    TensorRT-backed YOLOv8 detector for person class.

    Engine requirements:
      - Compiled with: yolo export format=engine imgsz=640 half=True
      - GPU SKU-specific: must be rebuilt when GPU changes
      - Input: (B, 3, 640, 640) — normalized float16 (mean=0, std=1 via built-in preprocess)
      - Output: (B, 84, 8400) — YOLO detection head (before NMS)

    NMS:
      - Applied post-inference with confidence threshold and IoU threshold.
      - Only class 0 (person) detections are retained for TRINETRA.
    """

    PERSON_CLASS_ID = 0
    DEFAULT_CONF_THRESHOLD = 0.35
    DEFAULT_IOU_THRESHOLD = 0.45
    INPUT_SIZE = (640, 640)

    def __init__(
        self,
        engine_path: str,
        conf_threshold: float = DEFAULT_CONF_THRESHOLD,
        iou_threshold: float = DEFAULT_IOU_THRESHOLD,
    ):
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self._engine = self._load_engine(engine_path)
        logger.info(f"YOLOv8 TensorRT engine loaded from {engine_path}")

    def _load_engine(self, engine_path: str):
        """
        Load TensorRT engine via ONNX Runtime TRT provider.
        Falls back to ONNX CPU if TRT unavailable (e.g., development machine).
        """
        try:
            import onnxruntime as ort
            providers = []

            try:
                import tensorrt  # noqa: F401
                providers.append((
                    "TensorrtExecutionProvider",
                    {
                        "trt_engine_cache_enable": True,
                        "trt_engine_cache_path": "/tmp/trt_cache",
                        "trt_fp16_enable": True,
                    },
                ))
                logger.info("Using TensorRT Execution Provider.")
            except ImportError:
                logger.warning("TensorRT not available. Using CUDAExecutionProvider.")

            providers.append("CUDAExecutionProvider")
            providers.append("CPUExecutionProvider")

            # For actual TRT engine files, use ultralytics inference directly
            # This path handles the ONNX→TRT via ONNX Runtime's TRT EP
            onnx_path = engine_path.replace(".engine", ".onnx")
            session = ort.InferenceSession(onnx_path, providers=providers)
            return session

        except Exception as e:
            logger.error(f"Failed to load detection engine: {e}")
            raise

    def _preprocess_batch(self, frames: list[np.ndarray]) -> np.ndarray:
        """
        Preprocess a batch of BGR frames into the model's input tensor.
        Output: (B, 3, 640, 640) float32, normalized [0, 1].
        """
        batch = []
        for frame in frames:
            resized = cv2.resize(frame, self.INPUT_SIZE, interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            normalized = rgb.astype(np.float32) / 255.0
            chw = np.transpose(normalized, (2, 0, 1))  # HWC → CHW
            batch.append(chw)
        return np.stack(batch, axis=0)

    def _postprocess(self, outputs: np.ndarray, frame_idx: int) -> list[Detection]:
        """
        Parse raw YOLO output (1, 84, 8400) for a single frame.
        Apply NMS, filter to person class only.
        """
        # outputs shape: (84, 8400) — [cx, cy, w, h, cls0_conf, ..., cls79_conf]
        output = outputs[frame_idx]  # (84, 8400)
        boxes = output[:4, :].T       # (8400, 4) — cx, cy, w, h
        class_scores = output[4:, :].T  # (8400, 80)

        # Extract person class confidence
        person_scores = class_scores[:, self.PERSON_CLASS_ID]
        mask = person_scores > self.conf_threshold

        if not mask.any():
            return []

        filtered_boxes = boxes[mask]
        filtered_scores = person_scores[mask]

        # Convert cx, cy, w, h → x1, y1, x2, y2 (normalized)
        x1 = filtered_boxes[:, 0] - filtered_boxes[:, 2] / 2
        y1 = filtered_boxes[:, 1] - filtered_boxes[:, 3] / 2
        x2 = filtered_boxes[:, 0] + filtered_boxes[:, 2] / 2
        y2 = filtered_boxes[:, 1] + filtered_boxes[:, 3] / 2

        # NMS via OpenCV (CPU, but fast for small N)
        bboxes_for_nms = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
        indices = cv2.dnn.NMSBoxes(
            bboxes_for_nms.tolist(),
            filtered_scores.tolist(),
            self.conf_threshold,
            self.iou_threshold,
        )

        detections = []
        for idx in (indices.flatten() if len(indices) else []):
            detections.append(Detection(
                bbox=[float(x1[idx]), float(y1[idx]), float(x2[idx]), float(y2[idx])],
                confidence=float(filtered_scores[idx]),
                class_id=self.PERSON_CLASS_ID,
            ))

        return detections

    def detect_batch(self, frames: list[np.ndarray]) -> list[list[Detection]]:
        """
        Run batch inference. Returns a list of detection lists per frame.
        Gracefully handles empty frames and returns [] on inference error.
        """
        if not frames:
            return []

        try:
            input_tensor = self._preprocess_batch(frames)
            input_name = self._engine.get_inputs()[0].name
            raw_outputs = self._engine.run(None, {input_name: input_tensor})[0]
            # raw_outputs shape: (B, 84, 8400)
            return [self._postprocess(raw_outputs, i) for i in range(len(frames))]

        except Exception as e:
            logger.error(f"Detection inference failed: {e}")
            return [[] for _ in frames]   # Fail open — don't crash; return empty
