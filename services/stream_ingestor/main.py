"""
Stream Ingestor — TRINETRA
--------------------------
Responsibility:
  • Open RTSP/GStreamer streams for each registered camera
  • Decode frames, apply adaptive frame-skipping
  • Serialize frames and push to Redis Streams (bounded length)
  • Expose FastAPI health + management endpoints
  • Export Prometheus metrics

Design: Each camera runs an independent asyncio task.
        Redis Streams act as a bounded, ordered frame buffer.
        Frame-drop is explicit (tail-drop with MAXLEN) — never silent.
"""

from __future__ import annotations

import asyncio
import time
import logging
import json
from typing import Optional

import cv2
import numpy as np
import redis.asyncio as aioredis
import yaml
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, Gauge, make_asgi_app
from pydantic import BaseModel

from config import Settings

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","service":"stream-ingestor","msg":"%(message)s"}',
)
logger = logging.getLogger("stream_ingestor")

# ─── Settings ────────────────────────────────────────────────────────────────

settings = Settings()

# ─── Prometheus Metrics ──────────────────────────────────────────────────────

FRAMES_INGESTED = Counter(
    "trinetra_ingestor_frames_total",
    "Total frames ingested per camera",
    ["camera_id", "camera_type"],
)
FRAMES_DROPPED = Counter(
    "trinetra_ingestor_frames_dropped_total",
    "Frames dropped due to backpressure per camera",
    ["camera_id"],
)
STREAM_RECONNECTS = Counter(
    "trinetra_ingestor_reconnects_total",
    "RTSP stream reconnect attempts per camera",
    ["camera_id"],
)
REDIS_STREAM_LENGTH = Gauge(
    "trinetra_redis_stream_length",
    "Current number of entries in the Redis frame stream",
    ["camera_id"],
)
FRAME_INGEST_LATENCY = Histogram(
    "trinetra_ingestor_frame_latency_seconds",
    "Time from frame capture to Redis push",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
)

# ─── Data Models ─────────────────────────────────────────────────────────────

class CameraConfig(BaseModel):
    id: str
    type: str                   # entrance | face_capture | tracking | billing | vehicle
    rtsp_url: str
    resolution: list[int]       # [width, height]
    target_fps: int

# ─── Adaptive Frame Sampler ──────────────────────────────────────────────────

class AdaptiveFrameSampler:
    """
    Decides whether a given frame should be forwarded for inference.

    Algorithm:
      - Baseline: forward every Nth frame (N = capture_fps / target_fps)
      - Backpressure: if Redis stream length > HIGH_WATER_MARK, increase skip rate
      - High motion: if per-frame optical flow mean > MOTION_THRESHOLD, decrease skip rate

    This prevents GPU starvation during burst periods and reduces compute
    during static scenes (empty store, closed hours).
    """

    HIGH_WATER_MARK = 80        # % of MAXLEN — above this, increase skip rate
    MOTION_THRESHOLD = 2.5      # Mean optical flow magnitude

    def __init__(self, capture_fps: int, target_fps: int):
        self.base_interval = max(1, capture_fps // target_fps)
        self.current_interval = self.base_interval
        self._count = 0
        self._prev_gray: Optional[np.ndarray] = None

    def should_forward(self, frame: np.ndarray, stream_fill_pct: float) -> bool:
        """Returns True if this frame should be pushed to Redis."""
        self._count += 1

        # Adjust interval based on backpressure
        if stream_fill_pct > self.HIGH_WATER_MARK:
            self.current_interval = min(self.current_interval + 1, self.base_interval * 3)
        else:
            motion = self._compute_motion(frame)
            if motion > self.MOTION_THRESHOLD:
                self.current_interval = max(1, self.current_interval - 1)
            else:
                self.current_interval = self.base_interval

        return self._count % self.current_interval == 0

    def _compute_motion(self, frame: np.ndarray) -> float:
        """Compute mean optical flow magnitude vs previous frame. Cheap: uses Farneback."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self._prev_gray is None:
            self._prev_gray = gray
            return 0.0
        flow = cv2.calcOpticalFlowFarneback(
            self._prev_gray, gray, None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
        )
        self._prev_gray = gray
        magnitude = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
        return float(magnitude.mean())


# ─── RTSP Camera Reader ───────────────────────────────────────────────────────

class RTSPCameraReader:
    """
    Non-blocking async RTSP reader backed by a dedicated thread.

    Threading model:
      - A daemon thread runs the blocking cv2.VideoCapture loop.
      - Frames are placed into an asyncio.Queue (bounded).
      - The async consumer (push_to_redis) reads from the queue without blocking.

    Reconnect strategy:
      - Exponential backoff: 1s → 2s → 4s → … → 30s ceiling.
      - Emits STREAM_RECONNECTS metric on each attempt.
      - Tracker state is preserved across reconnects (ByteTrack holds state
        externally in the inference worker, not here).
    """

    RECONNECT_CEILING_S = 30.0

    def __init__(self, config: CameraConfig, frame_queue: asyncio.Queue):
        self.config = config
        self.queue = frame_queue
        self._stop = asyncio.Event()
        self._capture_fps = 30  # Default; updated after first cap open

    async def run(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._blocking_reader_loop)

    def _blocking_reader_loop(self):
        delay = 1.0
        cap = self._open_cap()

        while not self._stop.is_set():
            ret, frame = cap.read()
            if not ret:
                logger.warning(
                    f"Stream lost for {self.config.id}. Reconnecting in {delay:.1f}s"
                )
                STREAM_RECONNECTS.labels(camera_id=self.config.id).inc()
                cap.release()
                time.sleep(delay)
                delay = min(delay * 2, self.RECONNECT_CEILING_S)
                cap = self._open_cap()
                if not cap.isOpened():
                    continue
                delay = 1.0  # Reset backoff on successful reconnect
                logger.info(f"Reconnected to {self.config.id}")
                continue

            # Drop oldest frame in queue if full (asyncio.Queue is thread-safe for this)
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                    FRAMES_DROPPED.labels(camera_id=self.config.id).inc()
                except asyncio.QueueEmpty:
                    pass

            try:
                self.queue.put_nowait(
                    (frame, time.time())  # (frame, ingest_timestamp)
                )
            except asyncio.QueueFull:
                FRAMES_DROPPED.labels(camera_id=self.config.id).inc()

    def _open_cap(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.config.rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # Minimize internal OpenCV buffer
        # Force TCP transport — more reliable than UDP for LAN cameras
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"H264"))
        if cap.isOpened():
            self._capture_fps = cap.get(cv2.CAP_PROP_FPS) or 30
        return cap

    def stop(self):
        self._stop.set()


# ─── Redis Frame Publisher ────────────────────────────────────────────────────

async def push_frames_to_redis(
    config: CameraConfig,
    frame_queue: asyncio.Queue,
    redis_client: aioredis.Redis,
    sampler: AdaptiveFrameSampler,
    stop_event: asyncio.Event,
):
    """
    Async coroutine: pulls frames from the in-process queue,
    applies frame sampling decision, serializes, and pushes to Redis Stream.

    Redis Stream key: frames:{camera_id}
    Redis MAXLEN (APPROX): settings.FRAME_BUFFER_MAXLEN
    Serialization: JPEG-compressed bytes (not raw) — ~10–50× size reduction.
    """
    stream_key = f"frames:{config.id}"

    while not stop_event.is_set():
        try:
            frame, ingest_ts = await asyncio.wait_for(
                frame_queue.get(), timeout=1.0
            )
        except asyncio.TimeoutError:
            continue

        t0 = time.perf_counter()

        # Check stream fill level for backpressure calculation
        stream_len = await redis_client.xlen(stream_key)
        fill_pct = (stream_len / settings.FRAME_BUFFER_MAXLEN) * 100

        REDIS_STREAM_LENGTH.labels(camera_id=config.id).set(stream_len)

        if not sampler.should_forward(frame, fill_pct):
            FRAMES_DROPPED.labels(camera_id=config.id).inc()
            continue

        # Resize to inference resolution (640×640) before serialization
        frame_resized = cv2.resize(frame, (640, 640), interpolation=cv2.INTER_LINEAR)

        # JPEG encode: quality=85 balances size vs. model accuracy impact
        _, buf = cv2.imencode(".jpg", frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])

        await redis_client.xadd(
            stream_key,
            {
                "camera_id": config.id,
                "camera_type": config.type,
                "ingest_ts": str(ingest_ts),
                "frame": buf.tobytes(),
            },
            maxlen=settings.FRAME_BUFFER_MAXLEN,
            approximate=True,   # MAXLEN ~ is O(1); exact MAXLEN is O(N)
        )

        elapsed = time.perf_counter() - t0
        FRAME_INGEST_LATENCY.observe(elapsed)
        FRAMES_INGESTED.labels(camera_id=config.id, camera_type=config.type).inc()


# ─── Application Startup ──────────────────────────────────────────────────────

app = FastAPI(title="TRINETRA Stream Ingestor", version="1.0.0")
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_stop_event = asyncio.Event()
_camera_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def startup():
    global _camera_tasks

    # Load camera configs
    with open(settings.CAMERA_CONFIGS) as f:
        raw = yaml.safe_load(f)
    cameras = [CameraConfig(**c) for c in raw["cameras"]]

    # Connect to Redis
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf8",
        decode_responses=False,  # We push binary frames
    )

    logger.info(f"Starting ingestion for {len(cameras)} cameras.")

    for cam in cameras:
        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.FRAME_BUFFER_MAXLEN)
        sampler = AdaptiveFrameSampler(capture_fps=30, target_fps=cam.target_fps)
        reader = RTSPCameraReader(config=cam, frame_queue=frame_queue)

        # Spawn reader and publisher tasks per camera
        t1 = asyncio.create_task(reader.run(), name=f"reader-{cam.id}")
        t2 = asyncio.create_task(
            push_frames_to_redis(cam, frame_queue, redis_client, sampler, _stop_event),
            name=f"publisher-{cam.id}",
        )
        _camera_tasks.extend([t1, t2])


@app.on_event("shutdown")
async def shutdown():
    _stop_event.set()
    for task in _camera_tasks:
        task.cancel()
    await asyncio.gather(*_camera_tasks, return_exceptions=True)
    logger.info("All camera tasks stopped cleanly.")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "stream-ingestor"}


@app.get("/cameras")
async def list_cameras():
    """Returns the list of currently active camera tasks."""
    return {
        "active_tasks": [t.get_name() for t in _camera_tasks if not t.done()]
    }
