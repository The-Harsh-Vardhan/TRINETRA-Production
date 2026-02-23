# TRINETRA — Production Engineering Design Document

> **Classification:** Internal Engineering Reference  
> **Author Role:** Principal Engineer, Computer Vision & AI  
> **System:** TRINETRA — Targeted Retail Insights via NETworked Real-time Analytics  
> **Status:** Prototype → Production Hardening Blueprint  
> **Date:** February 2026

---

## Executive Summary

TRINETRA in its current form is a **research-grade, single-process, single-machine prototype** with 8 independent Python modules wired together implicitly. Each module assumes ideal conditions: clean frames, available GPU, stable camera streams, and no concurrency. This document provides an engineering-grade redesign plan that transforms TRINETRA into a production system capable of handling:

- 8+ simultaneous RTSP camera streams
- Sub-200ms end-to-end inference latency per stream
- Fault-isolated, independently deployable processing pipelines
- Biometrically sensitive data with encryption and access control
- Horizontal scalability for multi-store deployments

---

## 1. System Architecture Redesign

### 1.1 Monolith vs Microservices Decision

**Decision: Hybrid — Modular Monolith per Node + Microservices for Cross-Cutting Concerns**

The research prototype is effectively a strict monolith: all 8 modules share a Python process, memory space, and GPU context.

**Why not pure microservices?** Full microservices sends raw frames over HTTP/gRPC. At 8 cameras × 25fps × ~500KB per frame ≈ 1GB/sec — unviable.

**The correct decomposition:**

```
┌──────────────────────────────────────────────────────────────────┐
│          TRINETRA Node (per physical camera group)               │
│  [Stream Ingestor] → Redis Streams → [Detection/Track Worker]   │
│                                              ↓                   │
│                                     [Re-ID & Face Worker]       │
│                                              ↓                   │
│                          Redis/Kafka Metadata Events             │
└──────────────────────────────────────────────────────────────────┘
         ↓                                   ↓
   [Analytics Svc]              [Identity Registry Service]
   (Behavioral, Heatmaps)       (PostgreSQL + pgvector)
         ↓                                   ↓
             [Dashboard API & Alerting Service]
```

### 1.2 Component Boundary Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Detection + Tracking | Colocated in same process | Share YOLO GPU context, zero copy |
| Re-ID / Face Recognition | Separate worker process | Different GPU memory lifecycle |
| Analytics, OCR | Async microservice | Latency-tolerant; can lag without dropping frames |
| Identity DB | Centralized (Qdrant + Postgres) | Cross-camera consistency requires single source of truth |
| Inter-service messaging | Redis Streams + Kafka | Redis for low-latency buffering; Kafka for durable cross-service events |

### 1.3 Stateless vs Stateful Components

| Component | Stateful? | State Location |
|---|---|---|
| Stream Ingestor | No | N/A |
| Detection Worker | No | N/A |
| ByteTrack Tracker | **Yes** | In-process (checkpoint to Redis) |
| Face Recognition | No | External DB |
| Behavioral Analytics | **Yes** | Time-series DB |
| Identity Registry | **Yes** | PostgreSQL + Qdrant |

> [!IMPORTANT]
> ByteTrack holds Kalman filter state **per camera** in-memory. Crash = track IDs reset → ID switches. Mitigation: checkpoint tracker state to Redis every N frames.

---

## 2. Model Serialization & Inference Strategy

### 2.1 Format Decision

| Format | Speed | Portability | Deployment Risk |
|---|---|---|---|
| `.pth` (PyTorch) | Baseline | Python-only | High |
| ONNX | 1.5–2× | Cross-framework | Medium |
| TensorRT (`.engine`) | 3–8× | NVIDIA GPU-only | GPU-SKU specific |

**Decision:** ONNX for portability; TensorRT for latency-critical paths (detection + face embedding).

### 2.2 Export Code

```python
from ultralytics import YOLO

def export_yolo_to_tensorrt(model_path: str, img_size: int = 640):
    model = YOLO(model_path)
    model.export(
        format="engine",
        imgsz=img_size,
        half=True,       # FP16 — halves memory, ~2× speed
        dynamic=False,   # Static batch for predictable latency
        workspace=4      # GB of TRT optimizer workspace
    )
```

```python
def export_arcface_to_onnx(model_path: str, output_path: str):
    import torch, onnx
    dummy = torch.randn(1, 3, 112, 112)
    torch.onnx.export(
        face_model.model, dummy, output_path,
        input_names=["face_crop"], output_names=["embedding"],
        opset_version=17,
        dynamic_axes={"face_crop": {0: "batch_size"}}
    )
    onnx.checker.check_model(onnx.load(output_path))
```

### 2.3 Batch vs Streaming Inference

| Mode | Use Case |
|---|---|
| Streaming (1 frame) | Billing camera — latency critical |
| Micro-batch (N=4, 20ms timeout) | Detection across camera group |
| Async batch | OCR, captioning — non-real-time |

### 2.4 Adaptive Frame Sampling

```python
class AdaptiveFrameSampler:
    """Skip frames based on backpressure AND motion score."""
    HIGH_WATER_MARK = 80     # % of Redis MAXLEN — above this, increase skip
    MOTION_THRESHOLD = 2.5   # Mean optical flow magnitude

    def should_forward(self, frame, stream_fill_pct: float) -> bool:
        if stream_fill_pct > self.HIGH_WATER_MARK:
            self.current_interval = min(self.current_interval + 1, self.base_interval * 3)
        else:
            motion = self._compute_motion(frame)
            self.current_interval = max(1, self.base_interval - (1 if motion > self.MOTION_THRESHOLD else 0))
        self.frame_count += 1
        return self.frame_count % self.current_interval == 0
```

---

## 3. Real-Time Video Processing Constraints

### 3.1 Frame Buffering Strategy

```python
class RTSPIngestor:
    """
    Decoupled RTSP reader: network I/O thread separate from processing.
    Buffer bounded: prevents memory explosion on slow consumers.
    Reconnect: exponential backoff 1s → 30s ceiling.
    """
```

**Key parameters:**
- `BUFFER_SIZE = 30` — ~1 second at 30fps; tune per deployment
- `cv2.CAP_PROP_BUFFERSIZE = 1` — minimize OpenCV internal buffer
- TCP transport (`rtsp_transport=tcp`) — eliminates UDP packet loss

### 3.2 Backpressure Handling

| Source | Mitigation |
|---|---|
| Slow GPU inference | Adaptive skipping + bounded Redis queue |
| Slow DB writes | Write to Kafka first; async batch flush to Postgres |
| Re-ID model slow | Re-ID runs async; tracker emits `UNKNOWN_ID` until resolved |

### 3.3 Dropped Frame Policy

```
Safety-Critical (billing, entrance counting):  NEVER drop
Analytics (heatmaps, dwell):                   Drop freely
Secondary (emotion, attire):                   Drop aggressively (1fps or on-demand)
```

### 3.4 Multi-Camera Synchronization

```python
@dataclass
class FramePacket:
    camera_id: str
    frame_index: int
    ingest_ts: float               # System time at ingest
    frame_ts: Optional[float]      # Camera-reported timestamp (if available)

    @property
    def effective_ts(self) -> float:
        return self.frame_ts if self.frame_ts else self.ingest_ts
```

Cross-camera correlation uses `effective_ts` with ±500ms tolerance window.

---

## 4. Concurrency & Scaling Challenges

### 4.1 GPU Resource Budget (RTX 3080, 10GB VRAM)

| Model | VRAM (FP16) |
|---|---|
| YOLOv8-m TRT | ~900 MB |
| ArcFace-R50 TRT | ~400 MB |
| Frame batch buffers | ~800 MB |
| CUDA workspace | ~1000 MB |
| **Total allocated** | **~3.1 GB** (~31% utilization) |

### 4.2 Worker Pool Design

```python
class DetectionWorkerPool:
    """
    One worker process per camera group.
    Each worker owns a CUDA context independently.
    mp_context="spawn" required — fork breaks GPU contexts.
    """
    def __init__(self, num_workers: int, gpu_ids: list[int], model_path: str):
        self.pool = ProcessPoolExecutor(
            max_workers=num_workers,
            mp_context=mp.get_context("spawn"),
            initializer=_worker_init,
            initargs=(model_path, gpu_ids)
        )
```

### 4.3 Priority-Aware Inference Queue

```python
class PriorityInferenceQueue:
    PRIORITY_MAP = {
        "billing": 0,    # Highest — financial consequence
        "entrance": 1,
        "tracking": 2,
        "behavioral": 3,
        "emotion": 4,    # Lowest — fully async
    }
```

---

## 5. Memory & Compute Stability

### 5.1 GPU OOM Guard

```python
class GPUModelManager:
    def load(self, name: str, model_factory, expected_mb: float):
        free_mem = torch.cuda.mem_get_info(self.device)[0]
        if expected_mb * 1024**2 > free_mem * 0.9:
            raise RuntimeError(f"Insufficient VRAM to load '{name}'")
        return model_factory()

    @contextmanager
    def inference_guard(self):
        try:
            yield
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            gc.collect()
            logging.error("GPU OOM — skipping batch.")
```

### 5.2 Tiled Detection for Dense Scenes

```python
def tiled_detection(detector, frame, tile_size=640, overlap=0.2):
    """Divide into overlapping tiles for crowded scenes. Merge with NMS."""
    stride = int(tile_size * (1 - overlap))
    detections = []
    for y in range(0, frame.shape[0], stride):
        for x in range(0, frame.shape[1], stride):
            tile = frame[y:y+tile_size, x:x+tile_size]
            result = detector.infer(tile)
            for det in result:
                det.x1 += x; det.y1 += y; det.x2 += x; det.y2 += y
                detections.append(det)
    return non_max_suppression(detections, iou_threshold=0.5)
```

---

## 6. Fault Tolerance & Failure Modes

### Failure Matrix

| Failure | Root Cause | System Symptoms | Mitigation |
|---|---|---|---|
| Camera stream failure | Network drop, camera reboot | `STREAM_RECONNECTS` metric rises, `FRAMES_INGESTED` plateaus | Exponential backoff reconnect (1s→30s); ByteTrack state preserved |
| Corrupted frame | UDP packet loss, partial decode | `cv2.imdecode` returns None | Frame validation: skip if `frame is None` or `std < 5` or mean < 2/> 253 |
| GPU unavailable | Thermal shutdown, driver crash | `CUDA error`; entire processing node fails | CPU ONNX fallback at 5fps; alert ops |
| Inference timeout | Thermal throttle, competing process | Latency spike >200ms | `concurrent.futures.ThreadPoolExecutor` with 100ms timeout |
| Service crash/restart | Unhandled exception, OOM | Redis XPENDING accumulates; Kafka consumer lag grows | `xautoclaim` on startup; Docker `restart: unless-stopped` |
| Partial pipeline failure | Re-ID service down | Tracks without customer identity | Degraded mode: emit `UNKNOWN_IDENTITY`; analytics continues |

### Defensive Code: Camera Reconnect

```python
def _handle_reconnect(self, cap: cv2.VideoCapture):
    delay = 1.0
    while not self._stop.is_set():
        time.sleep(delay)
        cap.release()
        cap.open(self.url, cv2.CAP_FFMPEG)
        if cap.isOpened():
            logging.info(f"[{self.camera_id}] Reconnected.")
            return
        delay = min(delay * 2, 30.0)
```

### Defensive Code: Degraded Mode Pipeline

```python
class PipelineOrchestrator:
    def process(self, track_event):
        identity = (
            self.reid_service.lookup(track_event.embedding)
            if self.reid_service.is_healthy()
            else Identity(id="UNKNOWN", confidence=0.0, source="degraded_mode")
        )
        self.analytics_service.record(track_event, identity)
```

---

## 7. Identity Tracking & Re-Identification Risks

### 7.1 ID Switch Reconciliation

```python
class TrackIDReconciler:
    COSINE_THRESHOLD = 0.65
    TEMPORAL_WINDOW_S = 10.0

    def try_reconcile(self, new_track, lost_tracks: list) -> str:
        for candidate in [t for t in lost_tracks if time.time() - t.last_seen < self.TEMPORAL_WINDOW_S]:
            if cosine_similarity(new_track.embedding, candidate.embedding) > self.COSINE_THRESHOLD:
                return candidate.track_id
        return new_track.track_id
```

### 7.2 Embedding EMA Update

```python
def update_stored_embedding(db, person_id: str, new_embedding, alpha: float = 0.1):
    """Online update with EMA: stored = (1-α)*stored + α*new. Only on high-confidence matches."""
    stored = db.get_embedding(person_id)
    updated = (1 - alpha) * stored + alpha * new_embedding
    db.store_embedding(person_id, updated / np.linalg.norm(updated))
```

### 7.3 Cross-Camera Embedding Normalization

```python
def normalize_embedding_per_camera(embedding, camera_id: str, stats: dict):
    """Subtract per-camera mean embedding to reduce lighting/viewpoint bias."""
    cam_mean = stats.get(camera_id, {}).get("mean_embedding")
    if cam_mean is not None:
        embedding = embedding - cam_mean
        embedding /= np.linalg.norm(embedding)
    return embedding
```

---

## 8. Logging, Monitoring & Observability

### 8.1 Structured Logging

```python
class StructuredLogger:
    def log(self, level: str, event: str, **kwargs):
        print(json.dumps({
            "ts": time.time(), "service": self.service,
            "camera_id": self.camera_id, "event": event,
            "level": level, **kwargs
        }))  # stdout → Fluentd/Vector → Loki
```

### 8.2 Key Prometheus Metrics

```python
FRAMES_PROCESSED   = Counter("trinetra_frames_total", ..., ["camera_id"])
DETECTION_LATENCY  = Histogram("trinetra_detection_latency_ms", ..., buckets=[10,25,50,100,200,500])
ACTIVE_TRACKS      = Gauge("trinetra_active_tracks", ..., ["camera_id"])
REID_MATCH_RATE    = Gauge("trinetra_reid_match_rate", ...)
GPU_VRAM_USED_MB   = Gauge("trinetra_gpu_vram_mb", ...)
DROPPED_FRAMES     = Counter("trinetra_dropped_frames_total", ..., ["camera_id"])
```

### 8.3 Alert Rules

| Metric | Threshold | Severity |
|---|---|---|
| `detection_latency_p99 > 200ms` | GPU throttling / OOM | HIGH |
| `reid_match_rate < 0.3` | Model drift / lighting change | MEDIUM |
| `dropped_frames_rate > 0.1` | Backpressure / slow consumer | MEDIUM |
| `active_tracks == 0 AND store_open` | Frame ingestion failure | HIGH |
| `gpu_vram_used > 9GB` | Pre-OOM warning | HIGH |

---

## 9. Privacy, Security & Abuse Prevention

### 9.1 Biometric Data Policy

| Data Type | Retention | Access |
|---|---|---|
| Raw face images | Max 24h | Inference service only; encrypted at rest |
| Face embeddings (512-dim) | Pseudonymized by UUID | AES-256; identity registry service only |
| License plates | SHA-256 hashed before storage | Decrypted only on lawful request |
| Video footage | 72h rolling window | Security manager; audit logged |

### 9.2 Encryption

```python
class BiometricVault:
    """Fernet (AES-128-CBC) for embedding storage. Key from AWS KMS — never hardcoded."""

    def encrypt_embedding(self, embedding: np.ndarray) -> bytes:
        return self.fernet.encrypt(embedding.astype(np.float32).tobytes())

    def decrypt_embedding(self, ciphertext: bytes) -> np.ndarray:
        return np.frombuffer(self.fernet.decrypt(ciphertext), dtype=np.float32)
```

### 9.3 Input Validation

```python
def validate_rtsp_url(url: str) -> bool:
    """Prevent SSRF — only allow known store network CIDRs."""
    import re
    return bool(re.match(r"^rtsp://(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.)", url))

def sanitize_ocr_output(text: str) -> str:
    """Prevent SQL injection from OCR bill text."""
    import re
    return re.sub(r"[^\w\s\.\,₹$]", "", text)
```

---

## 10. Deployment & Environment Management

### 10.1 Optimized Dockerfile (Detection Worker)

```dockerfile
# Stage 1: TensorRT base (NVIDIA official)
FROM nvcr.io/nvidia/tensorrt:23.10-py3 AS base

# Stage 2: Dependencies (cached separately)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

# Stage 3: Application
COPY src/ ./
RUN useradd -m -u 1001 trinetra && chown -R trinetra /app
USER trinetra

HEALTHCHECK --interval=30s CMD curl -f http://localhost:8080/health || exit 1
CMD ["python", "-m", "detection.worker"]
```

> [!CAUTION]
> TensorRT `.engine` files are GPU-SKU specific. Rebuild in CI/CD when target GPU changes. An engine built on RTX 3080 will fail on an A100.

### 10.2 CI/CD Pipeline

```
git push → CI pipeline:
  [lint + type check] ruff, mypy
  [unit tests] pytest (mocked GPU)
  [security scan] trivy
  [model export] yolo export on GPU runner
  [build + push] docker buildx
  [staging deploy] docker compose pull + up
  [smoke test] frame ingest + detection latency
  [production deploy] rolling update
```

---

## 11. Cost vs Performance Trade-offs

### 11.1 GPU Utilization vs Latency

| Batch N | GPU Utilization | P99 Latency |
|---|---|---|
| 1 | ~30% | 25ms |
| 4 ✓ | ~70% | 70ms |
| 8 | ~90% | 140ms |

N=4 recommended: 70% utilization with bounded latency.

### 11.2 Frame Rate vs Accuracy

| FPS | Detection Coverage | Re-ID Quality | Cost |
|---|---|---|---|
| 30fps | 100% | Excellent | High (full GPU) |
| 15fps ✓ | 95%+ | Good | Medium |
| 5fps | 85% | Acceptable | Low (CPU viable) |

### 11.3 Compute Budget Allocation (Single RTX 3080)

```
VRAM Budget (10GB):
├─ YOLOv8m-TRT (FP16):    900 MB
├─ ArcFace-R50-TRT (FP16): 400 MB
├─ PaddleOCR-ONNX:         300 MB
├─ CLIP ViT-B/32 (async):  600 MB
├─ Frame batch buffers:    800 MB
├─ CUDA workspace:        1000 MB
└─ Safety margin:         6000 MB  ← stable headroom
```

### 11.4 Deployment Cost Model

| Scale | Hardware | Est. Monthly Cost |
|---|---|---|
| 1 store, 8 cameras | 1× RTX 3080 (on-premise) | ~$0 (capex only) |
| 5 stores, 40 cameras | 5× edge nodes + cloud DB | ~$500/mo |
| 50 stores | Edge inference + cloud analytics | ~$3,000–8,000/mo |

> [!WARNING]
> Cloud GPU inference for real-time video is almost never cost-effective. An A10G on AWS costs ~$940/mo. A single RTX 3080 (~$700) pays for itself in <1 month vs cloud and delivers lower latency.

---

## Risk Register

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| GPU OOM crash on traffic spike | Medium | Critical | Memory budget manager + OOM guard |
| ID switches causing billing mismatch | High | High | Track reconciler + billing re-verification |
| GDPR violation from retained images | Low | Critical | 24h auto-delete policy + automated scan |
| Camera stream loss during peak hour | Medium | High | Resilient RTSP reader + alert |
| TRT engine incompatibility on new GPU | Medium | High | GPU-bound CI/CD build + versioned engine registry |
| Re-ID false positive (wrong customer) | Medium | High | Cosine threshold + spatiotemporal gate |
| Adversarial face spoofing | Low | Medium | Liveness detection module |
