"""
ArcFace Embedder — TRINETRA Inference Worker
---------------------------------------------
Generates 512-dimensional L2-normalized face embeddings using ArcFace
(ResNet-50 backbone) via ONNX Runtime with TensorRT provider.

Input: (B, 3, 112, 112) face crop — BGR, normalized to [-1, 1]
Output: (B, 512) L2-normalized float32 embedding

Why ArcFace over other face recognition models?
  - ArcFace loss (Additive Angular Margin) maximizes inter-class separation
    in the embedding space, making cosine similarity thresholding reliable.
  - Outperforms Softmax (CosFace, SphereFace) on LFW, AgeDB, IJB benchmarks.
  - Compact 512-dim output balances ANN index speed vs matching accuracy.
"""

from __future__ import annotations

import logging
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger("inference_worker.embedder")


class ArcFaceEmbedder:
    """
    TensorRT-backed ArcFace embedding generator.

    Normalization convention (InsightFace standard):
      pixel = (pixel / 127.5) - 1.0  →  range [-1, 1]
    This is NOT the same as ImageNet normalization.
    Using wrong normalization silently degrades embedding quality.
    """

    INPUT_SIZE = (112, 112)
    EMBEDDING_DIM = 512

    def __init__(self, engine_path: str):
        self._session = self._load_engine(engine_path)
        logger.info(f"ArcFace TRT engine loaded from {engine_path}")

    def _load_engine(self, engine_path: str):
        import onnxruntime as ort

        providers = []
        try:
            import tensorrt  # noqa: F401
            providers.append("TensorrtExecutionProvider")
        except ImportError:
            pass
        providers.extend(["CUDAExecutionProvider", "CPUExecutionProvider"])

        onnx_path = engine_path.replace(".engine", ".onnx")
        return ort.InferenceSession(onnx_path, providers=providers)

    def _preprocess(self, face_crops: list[np.ndarray]) -> np.ndarray:
        """
        Preprocess face crops to ArcFace input format.
        Assumes crops are already 112×112 BGR.
        """
        batch = []
        for crop in face_crops:
            if crop.shape[:2] != self.INPUT_SIZE:
                crop = cv2.resize(crop, self.INPUT_SIZE, interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            # InsightFace normalization: [-1, 1]
            normalized = (rgb.astype(np.float32) - 127.5) / 127.5
            chw = np.transpose(normalized, (2, 0, 1))  # HWC → CHW
            batch.append(chw)
        return np.stack(batch, axis=0)

    def _l2_normalize(self, embeddings: np.ndarray) -> np.ndarray:
        """L2-normalize along embedding dimension. Required for cosine similarity."""
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8
        return embeddings / norms

    def embed_batch(self, face_crops: list[np.ndarray]) -> list[np.ndarray]:
        """
        Generate ArcFace embeddings for a batch of face crops.

        Returns:
            List of (512,) L2-normalized float32 numpy arrays — one per crop.
            Returns empty list on error (fail open).

        Notes:
            Max practical batch size: 32 (VRAM bound on 8GB GPU).
            For >32 crops (crowded scenes), split into sub-batches.
        """
        if not face_crops:
            return []

        # Sub-batch to avoid VRAM OOM on crowded frames
        MAX_SUB_BATCH = 16
        all_embeddings: list[np.ndarray] = []

        for i in range(0, len(face_crops), MAX_SUB_BATCH):
            sub_batch = face_crops[i:i + MAX_SUB_BATCH]
            try:
                input_tensor = self._preprocess(sub_batch)
                input_name = self._session.get_inputs()[0].name
                raw = self._session.run(None, {input_name: input_tensor})[0]  # (B, 512)
                normalized = self._l2_normalize(raw)
                all_embeddings.extend([normalized[j] for j in range(len(sub_batch))])
            except Exception as e:
                logger.error(f"ArcFace inference error on sub-batch {i}: {e}")
                # Pad with zero embeddings for failed sub-batch
                all_embeddings.extend([np.zeros(self.EMBEDDING_DIM, dtype=np.float32)] * len(sub_batch))

        return all_embeddings

    def embed_single(self, face_crop: np.ndarray) -> Optional[np.ndarray]:
        """Convenience wrapper for single face embedding."""
        results = self.embed_batch([face_crop])
        return results[0] if results else None
