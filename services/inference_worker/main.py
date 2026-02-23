"""
Inference Worker — TRINETRA
----------------------------
Responsibility:
  • Pull serialized frames from Redis Streams (consumer group)
  • Micro-batch frames across cameras for GPU efficiency
  • Run YOLOv8 person detection (TensorRT engine)
  • Crop detected face regions → run ArcFace embedding (TensorRT engine)
  • Publish structured detection + embedding events to Kafka
  • Export Prometheus metrics for latency, throughput, GPU utilization

Concurrency model:
  Celery worker with gevent pool (I/O bound Redis/Kafka ops share a thread).
  CPU/GPU inference runs in the main thread — no thread-switching during TRT calls.

Scaling: Deploy N replicas; each joins the same Redis consumer group.
         Redis auto-distributes frame stream entries across consumers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

import cv2
import numpy as np
import redis
from celery import Celery
from confluent_kafka import Producer, KafkaException
from prometheus_client import Counter, Histogram, Gauge, start_http_server

from config import Settings
from detector import YOLOv8Detector
from embedder import ArcFaceEmbedder

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","service":"inference-worker","msg":"%(message)s"}',
)
logger = logging.getLogger("inference_worker")

# ─── Settings ────────────────────────────────────────────────────────────────

settings = Settings()

# ─── Prometheus Metrics ──────────────────────────────────────────────────────

DETECTION_LATENCY = Histogram(
    "trinetra_detection_latency_seconds",
    "YOLOv8 inference latency",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
EMBEDDING_LATENCY = Histogram(
    "trinetra_embedding_latency_seconds",
    "ArcFace embedding latency",
    buckets=[0.002, 0.005, 0.01, 0.025, 0.05],
)
FRAMES_PROCESSED = Counter(
    "trinetra_worker_frames_processed_total",
    "Frames fully processed by inference worker",
    ["camera_id"],
)
DETECTIONS_TOTAL = Counter(
    "trinetra_detections_total",
    "Person detections produced",
    ["camera_id"],
)
KAFKA_PUBLISH_ERRORS = Counter(
    "trinetra_kafka_publish_errors_total",
    "Failed Kafka publish attempts",
)
GPU_UTIL = Gauge(
    "trinetra_gpu_utilization_pct",
    "GPU utilization % (polled via nvidia-smi)",
)
GPU_VRAM_USED_MB = Gauge(
    "trinetra_gpu_vram_used_mb",
    "GPU VRAM used in MB",
)

# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class Detection:
    track_id: int
    bbox: list[float]       # [x1, y1, x2, y2] — normalized [0, 1]
    confidence: float
    class_id: int

@dataclass
class InferenceResult:
    camera_id: str
    camera_type: str
    ingest_ts: float
    worker_ts: float
    detections: list[Detection]
    embeddings: list[list[float]]   # Per-detection ArcFace embedding (512-dim)

# ─── Micro-Batch Accumulator ─────────────────────────────────────────────────

class MicroBatchAccumulator:
    """
    Accumulates frames from multiple cameras into a fixed-size batch.

    Strategy:
      - Wait up to BATCH_TIMEOUT_MS for BATCH_SIZE frames.
      - If the batch fills before timeout → flush immediately (throughput mode).
      - If timeout elapses with partial batch → flush anyway (latency mode).

    This ensures latency is bounded even when cameras have low activity.
    """

    def __init__(self, batch_size: int, timeout_ms: float):
        self.batch_size = batch_size
        self.timeout_ms = timeout_ms
        self._batch: list[dict] = []
        self._batch_start: float = time.perf_counter()

    def add(self, message: dict) -> bool:
        """Add a frame message. Returns True if batch is ready to flush."""
        self._batch.append(message)
        return self.is_ready()

    def is_ready(self) -> bool:
        elapsed_ms = (time.perf_counter() - self._batch_start) * 1000
        return len(self._batch) >= self.batch_size or elapsed_ms >= self.timeout_ms

    def flush(self) -> list[dict]:
        batch = self._batch.copy()
        self._batch.clear()
        self._batch_start = time.perf_counter()
        return batch


# ─── GPU Utilization Poller ──────────────────────────────────────────────────

def poll_gpu_metrics():
    """Poll nvidia-smi for utilization and VRAM usage. Runs in background thread."""
    import subprocess
    import threading

    def _poll():
        while True:
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                     "--format=csv,noheader,nounits"],
                    timeout=2,
                    text=True,
                )
                util, vram = [x.strip() for x in out.split(",")]
                GPU_UTIL.set(float(util))
                GPU_VRAM_USED_MB.set(float(vram))
            except Exception:
                pass
            time.sleep(5)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()


# ─── Kafka Producer ───────────────────────────────────────────────────────────

def build_kafka_producer() -> Producer:
    return Producer({
        "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "acks": "1",                    # Leader ack — balance durability vs latency
        "linger.ms": 5,                 # Micro-batch at Kafka level too
        "compression.type": "lz4",     # Reduce network bandwidth
        "retries": 5,
        "retry.backoff.ms": 100,
    })


def kafka_delivery_callback(err, msg):
    if err:
        logger.error(f"Kafka delivery failed: {err}")
        KAFKA_PUBLISH_ERRORS.inc()


# ─── Main Inference Loop ──────────────────────────────────────────────────────

def run_inference_worker():
    """
    Main blocking loop:
      1. Connect to Redis as a consumer group member.
      2. Poll frames using XREADGROUP.
      3. Accumulate micro-batches.
      4. Run YOLO detection + ArcFace embedding.
      5. Publish results to Kafka.
      6. ACK processed messages.
    """
    start_http_server(settings.METRICS_PORT)
    poll_gpu_metrics()

    redis_client = redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=False,
    )

    kafka_producer = build_kafka_producer()
    detector = YOLOv8Detector(engine_path=settings.YOLO_ENGINE_PATH)
    embedder = ArcFaceEmbedder(engine_path=settings.ARCFACE_ENGINE_PATH)

    accumulator = MicroBatchAccumulator(
        batch_size=settings.BATCH_SIZE,
        timeout_ms=settings.BATCH_TIMEOUT_MS,
    )

    # Create consumer group for each camera stream
    # (idempotent — MKSTREAM creates stream if not exists)
    streams = [f"frames:{cam_id}" for cam_id in _discover_camera_ids(redis_client)]

    for stream_key in streams:
        try:
            redis_client.xgroup_create(stream_key, "inference-workers", id="0", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.info(f"Consumer group already exists for {stream_key}")

    consumer_id = f"worker-{os.getpid()}"
    logger.info(f"Inference worker {consumer_id} started. Streams: {streams}")

    pending_ids: dict[str, list] = {}   # stream → [message_id, ...]

    while True:
        # XREADGROUP: blocking read, 50ms timeout, up to BATCH_SIZE messages per stream
        responses = redis_client.xreadgroup(
            "inference-workers",
            consumer_id,
            {s: ">" for s in streams},  # ">" = only new messages
            count=settings.BATCH_SIZE,
            block=50,       # ms — short block to allow timeout-based micro-batch flush
        )

        if responses:
            for stream_key_b, messages in responses:
                stream_key = stream_key_b.decode()
                for msg_id, fields in messages:
                    msg = {k.decode(): v for k, v in fields.items()}
                    msg["_stream_key"] = stream_key
                    msg["_msg_id"] = msg_id
                    accumulator.add(msg)

        if accumulator.is_ready():
            batch = accumulator.flush()
            _process_batch(batch, detector, embedder, kafka_producer, redis_client, pending_ids)


def _process_batch(
    batch: list[dict],
    detector: "YOLOv8Detector",
    embedder: "ArcFaceEmbedder",
    kafka_producer: Producer,
    redis_client: redis.Redis,
    pending_ids: dict,
):
    """Process one micro-batch: detect → embed → publish → ACK."""
    if not batch:
        return

    frames = []
    meta = []

    for msg in batch:
        buf = np.frombuffer(msg["frame"], dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            logger.warning(f"Corrupted frame from {msg.get('camera_id', 'unknown')} — skipping.")
            _validate_and_ack(msg, redis_client)
            continue
        frames.append(frame)
        meta.append(msg)

    if not frames:
        return

    # ── Detection Batch ──
    with DETECTION_LATENCY.time():
        batch_detections = detector.detect_batch(frames)   # Returns list[list[Detection]]

    # ── Per-detection Face Crop + Embedding ──
    all_results: list[InferenceResult] = []

    for i, (frame, detections, msg) in enumerate(zip(frames, batch_detections, meta)):
        face_crops = []
        for det in detections:
            x1, y1, x2, y2 = [int(c * dim) for c, dim in
                               zip(det.bbox, [frame.shape[1], frame.shape[0]] * 2)]
            crop = frame[y1:y2, x1:x2]
            if crop.size > 0:
                face_crops.append(cv2.resize(crop, (112, 112)))

        embeddings = []
        if face_crops:
            with EMBEDDING_LATENCY.time():
                embeddings = embedder.embed_batch(face_crops)  # Returns list[np.ndarray]
            embeddings = [e.tolist() for e in embeddings]

        result = InferenceResult(
            camera_id=msg["camera_id"].decode() if isinstance(msg["camera_id"], bytes) else msg["camera_id"],
            camera_type=msg.get("camera_type", b"unknown").decode() if isinstance(msg.get("camera_type"), bytes) else msg.get("camera_type", "unknown"),
            ingest_ts=float(msg.get("ingest_ts", 0)),
            worker_ts=time.time(),
            detections=detections,
            embeddings=embeddings,
        )
        all_results.append(result)

        FRAMES_PROCESSED.labels(camera_id=result.camera_id).inc()
        DETECTIONS_TOTAL.labels(camera_id=result.camera_id).inc(len(detections))

    # ── Kafka Publish ──
    for result in all_results:
        payload = json.dumps({
            **asdict(result),
            "detections": [asdict(d) for d in result.detections],
        }).encode()

        kafka_producer.produce(
            "trinetra.detections",
            key=result.camera_id.encode(),
            value=payload,
            on_delivery=kafka_delivery_callback,
        )
    kafka_producer.poll(0)  # Non-blocking flush of internal queue

    # ── ACK processed messages ──
    for msg in meta:
        _validate_and_ack(msg, redis_client)


def _validate_and_ack(msg: dict, redis_client: redis.Redis):
    stream_key = msg.get("_stream_key")
    msg_id = msg.get("_msg_id")
    if stream_key and msg_id:
        try:
            redis_client.xack(stream_key, "inference-workers", msg_id)
        except Exception as e:
            logger.warning(f"Failed to ACK message {msg_id}: {e}")


def _discover_camera_ids(redis_client: redis.Redis) -> list[str]:
    """Discover active frame streams from Redis key pattern."""
    keys = redis_client.keys("frames:*")
    return [k.decode().split(":", 1)[1] for k in keys] if keys else ["cam_default"]


if __name__ == "__main__":
    run_inference_worker()
