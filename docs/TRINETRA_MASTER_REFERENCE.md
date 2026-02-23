# TRINETRA — Master Production Reference

> **Version:** 1.0 | **Date:** February 2026 | **Classification:** Internal Engineering

---

## Table of Contents
1. [Architecture Diagram](#1-architecture-diagram)
2. [Folder Structure](#2-folder-structure)
3. [Data & Streaming Layer](#3-data--streaming-layer)
4. [Model Layer — Inference & Identity](#4-model-layer--inference--identity)
5. [Backend Microservices Architecture](#5-backend-microservices-architecture)
6. [Observability & Monitoring](#6-observability--monitoring)
7. [CI/CD with GitHub Actions](#7-cicd-with-github-actions)
8. [Frontend Dashboard](#8-frontend-dashboard)
9. [Performance & Stability Engineering](#9-performance--stability-engineering)
10. [Limitations & Risk Analysis](#10-limitations--risk-analysis)
11. [Production-Readiness Checklist](#11-production-readiness-checklist)

---

## 1. Architecture Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                     PHYSICAL CAMERA LAYER                                    ║
║  [Entrance]  [Face-Cap]  [Tracking×N]  [Billing]  [Vehicle]  [Emotion]      ║
╚═══════════════════════╦══════════════════════════════════════════════════════╝
                        ║ RTSP/TCP streams
                        ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║              INGESTION TIER  (CPU, horizontally scalable)                    ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │  Stream Ingestor Service  (1 replica per camera group)             │    ║
║  │  • Per-camera asyncio task   • Adaptive frame sampler              │    ║
║  │  • JPEG encode 640×640       • Reconnect w/ exp. backoff           │    ║
║  │  • Prometheus /metrics:8001  • FastAPI /health, /cameras           │    ║
║  └──────────────────────────┬──────────────────────────────────────────┘    ║
╚═════════════════════════════╬════════════════════════════════════════════════╝
                              ║ Redis XADD  frames:{cam_id}  MAXLEN~100
                              ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║               BUFFER TIER  (Redis Streams)                                   ║
║  • Ordered, bounded per-camera stream    • Consumer group isolation          ║
║  • Tail-drop under backpressure          • XPENDING replay on crash         ║
╚═════════════════════════════╦════════════════════════════════════════════════╝
                              ║ XREADGROUP  (consumer group: inference-workers)
                              ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║              INFERENCE TIER  (GPU, 1–N workers)                              ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │  Inference Worker Service                                          │    ║
║  │  • MicroBatch N=4 / 20ms timeout                                   │    ║
║  │  • YOLOv8-m TRT → person detection                                 │    ║
║  │  • ArcFace-R50 TRT → 512-dim embedding                             │    ║
║  │  • ByteTrack (in-process, per-camera state)                        │    ║
║  │  • GPU VRAM budget manager  • OOM guard                            │    ║
║  └──────────────────────────┬──────────────────────────────────────────┘    ║
╚═════════════════════════════╬════════════════════════════════════════════════╝
                              ║ Kafka PRODUCE  trinetra.detections (8 partitions)
                              ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║              EVENT BACKBONE  (Kafka)                                         ║
║  Topics:  trinetra.detections │ trinetra.identities │ trinetra.alerts       ║
║           trinetra.analytics  │ trinetra.journeys                            ║
╚════════╦═══════════════════════════════════════════╦═════════════════════════╝
         ║ Kafka CONSUME                             ║ Kafka CONSUME
         ▼                                           ▼
╔═════════════════════════════╗         ╔════════════════════════════════════╗
║  IDENTITY RESOLVER SERVICE  ║         ║  ANALYTICS SERVICE                 ║
║  • Qdrant HNSW ANN search   ║         ║  • Journey reconstruction          ║
║  • Cosine threshold 0.72    ║         ║  • Dwell time, heatmaps            ║
║  • Spatiotemporal gate      ║         ║  • VIP / repeat customer alerts    ║
║  • ActiveIdentityRegistry   ║         ║  • Kafka → Postgres time-series    ║
╚══════════════╦══════════════╝         ╚══════════════════════╦═════════════╝
               ║                                               ║
               ▼                                               ▼
╔══════════════════════════════════════════════════════════════════════════╗
║               STORAGE TIER                                                ║
║  PostgreSQL (journey/visit history)  │  Qdrant (face embeddings HNSW)   ║
║  Redis (active track state cache)    │  S3/MinIO (video archive 72h)    ║
╚═══════════════════════════════╦══════════════════════════════════════════╝
                                ║
                                ▼
╔══════════════════════════════════════════════════════════════════════════╗
║               API GATEWAY  (FastAPI)                                      ║
║  REST endpoints │ WebSocket real-time feed │ Auth (JWT)                   ║
╚═══════════════════════════════╦══════════════════════════════════════════╝
                                ║
                                ▼
╔══════════════════════════════════════════════════════════════════════════╗
║               FRONTEND DASHBOARD                                          ║
║  Real-time camera feeds │ Journey map │ Identity confidence │ Alerts      ║
╚══════════════════════════════════════════════════════════════════════════╝

                        OBSERVABILITY (cross-cutting)
                ┌─────────────────────────────────────┐
                │  Prometheus ← scrapes :8001/:8002/:8003
                │  Grafana ← dashboards + alerting    │
                │  Structured JSON logs → Loki        │
                └─────────────────────────────────────┘
```

---

## 2. Folder Structure

```
TRINETRA/
├── .github/
│   └── workflows/
│       ├── code_quality.yml          # Lint, type check, unit tests
│       ├── model_validation.yml      # Embedding + threshold regression tests
│       ├── docker_build.yml          # Multi-stage build + security scan
│       ├── deploy.yml                # Staging auto + production manual
│       └── load_test.yml             # Locust multi-camera burst simulation
│
├── services/
│   ├── stream_ingestor/
│   │   ├── main.py                   # FastAPI + per-camera asyncio tasks
│   │   ├── config.py                 # Pydantic settings
│   │   ├── sampler.py                # AdaptiveFrameSampler
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── inference_worker/
│   │   ├── main.py                   # Redis consumer group + batch loop
│   │   ├── detector.py               # YOLOv8 TRT wrapper
│   │   ├── embedder.py               # ArcFace TRT wrapper
│   │   ├── tracker.py                # ByteTrack per-camera state
│   │   ├── batch.py                  # MicroBatchAccumulator
│   │   ├── config.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── identity_resolver/
│   │   ├── main.py                   # Kafka consumer + Qdrant + gate
│   │   ├── gating.py                 # SpatiotemporalGate
│   │   ├── registry.py               # ActiveIdentityRegistry (TTL cache)
│   │   ├── confidence.py             # History-based confidence scorer
│   │   ├── config.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── analytics_service/
│   │   ├── main.py                   # Journey reconstruction + heatmaps
│   │   ├── journey.py                # JourneyTracker per customer
│   │   ├── heatmap.py                # Zone dwell-time aggregation
│   │   ├── alerts.py                 # VIP / repeat customer alerts
│   │   ├── config.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   └── api_gateway/
│       ├── main.py                   # FastAPI REST + WebSocket
│       ├── auth.py                   # JWT verification
│       ├── routers/
│       │   ├── cameras.py
│       │   ├── identities.py
│       │   ├── journeys.py
│       │   └── analytics.py
│       ├── Dockerfile
│       └── requirements.txt
│
├── frontend/
│   ├── index.html                    # Real-time dashboard (vanilla JS + WS)
│   ├── app.js
│   └── style.css
│
├── k8s/
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── stream-ingestor-deployment.yaml
│   ├── inference-worker-deployment.yaml
│   ├── identity-resolver-deployment.yaml
│   ├── analytics-deployment.yaml
│   ├── hpa.yaml                      # HorizontalPodAutoscaler
│   └── pdb.yaml                      # PodDisruptionBudget
│
├── tests/
│   ├── unit/
│   │   ├── test_sampler.py
│   │   ├── test_gating.py
│   │   └── test_confidence.py
│   ├── integration/
│   │   ├── test_pipeline.py          # End-to-end with mocked RTSP
│   │   └── test_reid.py
│   ├── model/
│   │   ├── test_embedding_consistency.py
│   │   ├── test_threshold_regression.py
│   │   └── test_drift_detection.py
│   └── load/
│       └── locustfile.py             # Multi-camera burst simulation
│
├── models/                           # TRT engine files (gitignored)
│   ├── yolov8m.engine
│   └── arcface_r50.engine
│
├── config/
│   └── cameras.yaml
│
├── prometheus/
│   └── prometheus.yml
│
├── grafana/
│   └── provisioning/
│
├── docs/
│   ├── ARCHITECTURE_OVERVIEW.md
│   ├── TRINETRA_MASTER_REFERENCE.md  ← this file
│   ├── PRODUCTION_DESIGN.md
│   ├── stream_ingestor_DESIGN.md
│   ├── inference_worker_DESIGN.md
│   └── identity_resolver_DESIGN.md
│
├── scripts/
│   ├── export_models.py              # .pt → TRT engine
│   ├── calibrate_reid.py             # Threshold calibration
│   └── seed_qdrant.py                # Bulk embed + upload gallery
│
├── docker-compose.yml
├── Makefile
├── .env.example
└── README.md
```

---

## 3. Data & Streaming Layer

### 3.1 Why Ingesting Raw Frames Directly into Inference Is Dangerous

```
Problem A — Head-of-Line Blocking:
  If Camera 1's inference is slow (crowded scene), Cameras 2–8 are blocked.
  One slow camera kills the entire system.

Problem B — Memory Pressure:
  A 4K frame = 25MB uncompressed. 8 cameras × 30fps × 25MB = 6GB/sec RAM write rate.
  GPU tensor buffers additionally require CUDA-pinned memory.

Problem C — GPU Starvation:
  Without buffering, preprocessing becomes synchronous with capture.
  GPU sits idle waiting for I/O instead of running inference.

Solution: Two-stage decoupling via Redis Streams.
  Stage 1 (Ingestor): RTSP → JPEG 640×640 → Redis XADD   [CPU, async]
  Stage 2 (Worker):   Redis XREADGROUP → TRT inference      [GPU, batch]
```

### 3.2 Camera Isolation Strategy

Each camera gets its **own Redis Stream key** (`frames:cam_01`, `frames:cam_02`...). No camera can fill another camera's buffer. Under XREADGROUP, the inference worker reads from ALL streams in a single round-robin call — ensuring no single camera monopolizes the GPU.

```
frames:cam_01  [▓▓▓▓▓▓░░░░]  ← 60% full — billing camera (high traffic)
frames:cam_02  [▓░░░░░░░░░]  ← 10% full — vehicle camera (low traffic)
frames:cam_03  [▓▓▓░░░░░░░]  ← 30% full — tracking camera

XREADGROUP COUNT=1 from EACH stream → balanced GPU scheduling
```

### 3.3 Frame Dropping Policy Under Overload

```
Priority Tier 1 — NEVER drop (billing, entrance counting):
  Billing counter: billing confirmation has financial consequence.
  Entrance: footfall count must be monotonic.
  Mitigation: separate dedicated Redis stream, reserved GPU time slice.

Priority Tier 2 — Adaptive drop (tracking, face capture):
  AdaptiveFrameSampler reduces rate when Redis fill > 80%.
  1-in-2, 1-in-3, 1-in-4 sampling based on fill level AND motion.

Priority Tier 3 — Aggressive drop (emotion, attire, vehicle):
  Run at 1fps or on-demand only.
  Redis XADD with MAXLEN=10 for these streams — hard cap.
```

### 3.4 Burst Frame Rate Handling

Most IP cameras produce burst frames (P-frames drop, then I-frame burst). Handle via:

```python
class BurstSuppressor:
    """Token bucket: allows up to BURST_TOKENS frames in rapid succession,
    then enforces TARGET_FPS rate."""
    def __init__(self, target_fps: int = 15, burst_tokens: int = 5):
        self.tokens = burst_tokens
        self.refill_rate = 1.0 / target_fps   # seconds per token
        self.last_refill = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.tokens + elapsed / self.refill_rate, 5.0)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False
```

### 3.5 Preprocessing Pipeline

```
Frame (JPEG bytes from Redis)
    ↓ cv2.imdecode → np.ndarray BGR
    ↓ validate_frame() [std_dev > 5, mean in (2, 253)]
    ↓ cv2.resize(640, 640) [interp = LINEAR]
    ↓ /255.0, CHW transpose
    ↓ np.stack → batch tensor (B, 3, 640, 640)
    ↓ TRT inference
    ↓ face crop regions → cv2.resize(112, 112)
    ↓ (crop - 127.5) / 127.5 [ArcFace convention]
    ↓ ArcFace TRT → (B, 512) L2-normalized
```

**GPU vs CPU preprocessing tradeoff:**

| Operation | GPU (CUDA/CuCV) | CPU | Decision |
|---|---|---|---|
| Decode JPEG | Fast (nvJPEG) | Adequate | CPU — avoids VRAM for I/O ops |
| Resize | Fast (TRT preprocess) | ~2ms | CPU — baked into model input |
| Normalize | Very fast | ~0.5ms | CPU — trivial math |
| Detection NMS | Required on GPU | 50ms | GPU via TRT built-in NMS |

**Preprocessing bottleneck → GPU starvation** occurs when the CPU decode loop is slower than TRT batch processing. Symptom: `GPU_UTIL < 40%` while frames are queued. Fix: separate decode threads from inference dispatch using `concurrent.futures.ThreadPoolExecutor`.

---

## 4. Model Layer — Inference & Identity

### 4.1 Detection + Embedding Latency Constraints

```
Budget: 200ms end-to-end per frame (ingestion → identity published)

  RTSP → Redis XADD:         ~5ms   (network + JPEG encode)
  Redis XREADGROUP:          ~2ms
  Preprocessing (CPU):       ~8ms
  YOLOv8-m TRT FP16:        ~18ms  (batch=4, 640×640)
  Face crop + resize:        ~3ms
  ArcFace-R50 TRT FP16:     ~12ms  (batch up to 16 crops)
  Qdrant HNSW search:        ~4ms
  Spatiotemporal gate:       ~0.1ms
  Kafka publish:             ~3ms
  ─────────────────────────────────
  Total:                     ~55ms  ✓ well within 200ms budget
```

### 4.2 Identity Association Logic

**Why precision > recall in identity tracking:**

A false merge (person A's track assigned to person B's identity) propagates incorrect billing data, journey records, and VIP alerts. This is a **catastrophic error** — harder to recover from than a missed match (which simply emits UNKNOWN_IDENTITY and loses that observation).

**Distance metric selection:**

```
ArcFace loss is defined in angular space:
  L = -log( e^(s·cos(θᵧᵢ + m)) / Σ e^(s·cos(θⱼ)) )

Therefore the embedding space maps identity to angle, not magnitude.
On the L2-normalized unit hypersphere:
  cosine_similarity(u, v) = u·v  (dot product of unit vectors)
  euclidean_distance(u, v) = √(2 - 2·cosine_similarity(u, v))

Both metrics produce identical ranking. Choose cosine for interpretability:
  cos > 0.95 → almost certainly same person
  cos > 0.72 → confident match (TRINETRA threshold)
  cos > 0.50 → weak match, flag for review
  cos < 0.50 → different person
```

**Threshold selection logic:**

```python
def calibrate_threshold(gallery: dict, val_pairs: list) -> float:
    """
    Find threshold maximizing F-beta score (beta < 1 penalizes false merges heavily).
    Beta = 0.5 gives precision 2× the weight of recall — precision-first policy.
    """
    from sklearn.metrics import fbeta_score
    import numpy as np

    thresholds = np.arange(0.50, 0.95, 0.005)
    best_t, best_score = 0.72, 0.0

    for t in thresholds:
        preds, labels = [], []
        for (emb_a, emb_b), same_person in val_pairs:
            sim = float(np.dot(emb_a, emb_b))
            preds.append(1 if sim > t else 0)
            labels.append(1 if same_person else 0)
        score = fbeta_score(labels, preds, beta=0.5, average="binary")
        if score > best_score:
            best_score, best_t = score, t

    return best_t
```

**Identity flicker mitigation** — History-based confidence scoring:

```python
class HistoryConfidenceScorer:
    """
    An identity assignment is only accepted if it is confirmed across
    WINDOW consecutive frames with avg cosine > THRESHOLD.
    Single-frame matches are never trusted.
    """
    WINDOW = 5
    THRESHOLD = 0.74

    def __init__(self):
        self._history: dict[int, list[tuple[str, float]]] = {}  # track_id → [(cust_id, score)]

    def record(self, track_id: int, candidate_id: str, score: float):
        buf = self._history.setdefault(track_id, [])
        buf.append((candidate_id, score))
        if len(buf) > self.WINDOW:
            buf.pop(0)

    def resolved_identity(self, track_id: int) -> tuple[str, float] | None:
        buf = self._history.get(track_id, [])
        if len(buf) < self.WINDOW:
            return None  # Not enough history yet
        # Majority vote across window
        from collections import Counter
        votes = Counter(cid for cid, _ in buf)
        top_id, top_count = votes.most_common(1)[0]
        if top_count < self.WINDOW // 2 + 1:
            return None  # No majority → ambiguous, emit UNKNOWN
        avg_score = sum(s for cid, s in buf if cid == top_id) / top_count
        if avg_score < self.THRESHOLD:
            return None
        return top_id, avg_score
```

### 4.3 Journey Tracking & State Management

**State per tracked identity:**

```python
@dataclass
class CustomerJourney:
    customer_id: str
    session_id: str             # UUID per store visit
    entry_ts: float
    last_seen_ts: float
    last_camera: str
    zones_visited: list[str]    # Ordered list of zone names
    dwell_per_zone: dict[str, float]  # zone → total seconds
    embedding_history: list     # Last 10 embeddings for EMA update
    confidence_score: float     # Rolling avg match confidence
```

**Why naive synchronization fails:**

```
Camera A records exit at T₀ = 10:05:00.123  (NTP-synced clock)
Camera B records entry at T₁ = 10:05:00.089  (NTP drift: -34ms)

Naive delta: T₁ - T₀ = -34ms → interpreted as "person arrived before leaving"
→ journey reconstruction breaks: negative dwell time, impossible transitions rejected

Fix: Use effective_ts with ±500ms tolerance window for cross-camera transitions.
     Treat any |delta| < 500ms as "simultaneous" — no transition penalty.
```

**Concept drift in identity models** occurs when a person's face appearance changes (haircut, beard, glasses) over weeks. Their stored embedding no longer matches their live face embedding.

```python
def ema_update(stored: np.ndarray, new: np.ndarray, alpha: float = 0.05) -> np.ndarray:
    """
    Exponential moving average update — slow adaptation (alpha=0.05).
    Only applied on HIGH-confidence matches (score > 0.85) to prevent
    gradual drift from mismatches corrupting the gallery.
    """
    updated = (1 - alpha) * stored + alpha * new
    return updated / np.linalg.norm(updated)
```

**Distributed state store strategy:**

| State Type | Store | TTL | Consistency |
|---|---|---|---|
| Active track (ByteTrack Kalman) | In-process + Redis checkpoint | 30s | Eventual |
| Customer journey (current session) | Redis hash | 4h | Eventual |
| Identity gallery (embeddings) | Qdrant (HNSW) | Permanent | Strong |
| Visit history (completed journeys) | PostgreSQL | Permanent | Strong |
| Analytics aggregates | TimescaleDB | 90 days | Strong |

---

## 5. Backend Microservices Architecture

### 5.1 Why Monolith Is Dangerous

```
Single-process failure modes in the TRINETRA POC:

1. Face recognition OOM → ALL camera streams stop (no fault isolation)
2. Slow CLIP captioning for Camera 5 → blocks entrance count for Camera 1
3. ByteTrack state is in-memory → any crash wipes all active track IDs
4. Shared GPU context → one model's memory leak kills everything
5. Cannot scale detection independently from analytics (tightly coupled)
```

### 5.2 Service Interaction Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                    REQUEST FLOW (happy path)                         │
│                                                                      │
│  [Camera]──RTSP──▶[Ingestor]──Redis──▶[Inference Worker]           │
│                                               │                      │
│                                      Kafka: trinetra.detections      │
│                                               │                      │
│                              ┌────────────────┴──────────┐          │
│                              ▼                            ▼          │
│                    [Identity Resolver]          [Analytics Service]  │
│                              │                            │          │
│                      Qdrant lookup              Journey reconstruct  │
│                              │                            │          │
│                    Kafka: trinetra.identities   Postgres write       │
│                              │                                       │
│                       [API Gateway]◀───────────────────────         │
│                              │                                       │
│                        WebSocket push                                │
│                              ▼                                       │
│                     [Frontend Dashboard]                             │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.3 Queue-Based Architecture & Backpressure

```
Camera → [Redis Stream, MAXLEN=100] → [Inference Worker]
                │                              │
          TAIL-DROP if full           ACK after processing
          FRAMES_DROPPED metric        XPENDING on crash

Inference Worker → [Kafka, retention=24h] → [Identity Resolver]
                              │
                    Consumer lag = natural buffer
                    No data loss, only delay
```

**Priority scheduling across camera types:**

```python
CAMERA_PRIORITY = {
    "billing":      0,   # Always first
    "entrance":     1,   # Footfall critical
    "face_capture": 2,   # Identity critical
    "tracking":     3,   # Journey analytics
    "vehicle":      4,
    "emotion":      5,   # Async / lowest priority
}
```

**Preventing retry storms:** Use exponential backoff with jitter on all Kafka consumer errors. Never retry synchronously in-process.

```python
async def consume_with_retry(consumer, max_retries=5):
    for attempt in range(max_retries):
        try:
            return await consumer.getmany(timeout_ms=50)
        except Exception as e:
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Consumer error (attempt {attempt+1}): {e}. Retry in {wait:.1f}s")
            await asyncio.sleep(wait)
    raise RuntimeError("Kafka consumer max retries exceeded")
```

### 5.4 Kubernetes Deployment Architecture

```
┌─── GPU Node Pool (NVIDIA taint: gpu=true) ──────────────────────────┐
│  inference-worker Pods × 2                                           │
│  • resources.requests.nvidia.com/gpu: 1                              │
│  • PriorityClass: high-priority                                      │
└─────────────────────────────────────────────────────────────────────┘

┌─── CPU Node Pool (general compute) ─────────────────────────────────┐
│  stream-ingestor    × 1 per camera group (StatefulSet)              │
│  identity-resolver  × 2–8 (HPA: scale on kafka_consumer_lag)        │
│  analytics-service  × 2                                              │
│  api-gateway        × 2                                              │
└─────────────────────────────────────────────────────────────────────┘

┌─── Stateful Services ───────────────────────────────────────────────┐
│  redis    (1 primary + 1 replica, PVC)                               │
│  kafka    (3-node cluster, PVC)                                      │
│  qdrant   (1 node, PVC — scale via Qdrant distributed mode)         │
│  postgres (1 primary + 1 replica, PVC)                               │
└─────────────────────────────────────────────────────────────────────┘
```

**GPU scheduling challenge:** Kubernetes cannot fractionally share a GPU across pods (without MPS). Each inference-worker pod gets exactly 1 GPU. For N cameras, estimate 1 GPU per 8–10 cameras at 15fps detection.

**Identity service scales differently:** It is Kafka consumer lag–driven, not CPU-driven. HPA uses custom metric (`kafka_consumer_group_lag`) via Prometheus Adapter, not CPU utilization.

**Cold start implication:** An inference-worker pod takes 30–60s to load TRT engines into GPU memory. During this window, Redis frames accumulate. XPENDING prevents loss but latency spikes. Use `startupProbe` with a 120s `failureThreshold` to prevent premature pod eviction during engine load.

---

## 6. Observability & Monitoring

### 6.1 Metrics Schema

```python
# All services — Prometheus instrumentation

# Ingestor
trinetra_ingestor_frames_total{camera_id, camera_type}           Counter
trinetra_ingestor_frames_dropped_total{camera_id}                Counter
trinetra_ingestor_reconnects_total{camera_id}                    Counter
trinetra_redis_stream_length{camera_id}                          Gauge
trinetra_ingestor_frame_latency_seconds                          Histogram

# Inference Worker
trinetra_detection_latency_seconds                               Histogram
trinetra_embedding_latency_seconds                               Histogram
trinetra_worker_frames_processed_total{camera_id}                Counter
trinetra_detections_total{camera_id}                             Counter
trinetra_gpu_utilization_pct                                     Gauge
trinetra_gpu_vram_used_mb                                        Gauge
trinetra_batch_fill_ratio                                        Histogram

# Identity Resolver
trinetra_reid_latency_seconds                                    Histogram
trinetra_qdrant_query_latency_seconds                            Histogram
trinetra_reid_matches_total{camera_id}                           Counter
trinetra_reid_unknowns_total{camera_id}                          Counter
trinetra_spatiotemporal_gate_rejections_total{reason}            Counter
trinetra_identity_flicker_total{customer_id}                     Counter
trinetra_false_merge_detected_total                              Counter
trinetra_active_identities                                       Gauge
trinetra_reid_confidence_score                                   Histogram

# Analytics
trinetra_journey_completed_total                                 Counter
trinetra_avg_dwell_time_seconds{zone}                            Gauge
trinetra_vip_alerts_total                                        Counter
```

### 6.2 Alert Rules

```yaml
# prometheus/alerts.yml

groups:
  - name: trinetra_critical
    rules:

    - alert: IdentityFlickerSpike
      # A customer's identity switches >3 times in 60s — threshold instability
      expr: rate(trinetra_identity_flicker_total[60s]) > 0.05
      for: 30s
      severity: critical
      annotations:
        summary: "Identity flicker rate too high — check Re-ID threshold or lighting change"

    - alert: FalseMergeDetected
      expr: increase(trinetra_false_merge_detected_total[5m]) > 0
      for: 0s
      severity: page          # Any false merge is a P0 event
      annotations:
        summary: "False identity merge detected — customer privacy at risk"

    - alert: QueueDepthCritical
      expr: trinetra_redis_stream_length > 90
      for: 60s
      severity: high
      annotations:
        summary: "Redis stream near MAXLEN — frames being dropped"

    - alert: InferenceLatencyViolation
      expr: histogram_quantile(0.99, trinetra_detection_latency_seconds_bucket) > 0.2
      for: 2m
      severity: high
      annotations:
        summary: "P99 detection latency > 200ms — GPU throttling or OOM"

    - alert: GPUStarvation
      expr: trinetra_gpu_utilization_pct < 20 AND trinetra_redis_stream_length > 50
      for: 2m
      severity: medium
      annotations:
        summary: "GPU idle but frames queued — preprocessing bottleneck"

    - alert: ReidMatchRateDrop
      expr: rate(trinetra_reid_matches_total[5m]) / rate(trinetra_worker_frames_processed_total[5m]) < 0.3
      for: 5m
      severity: medium
      annotations:
        summary: "Re-ID match rate < 30% — possible model drift or lighting change"

    - alert: KafkaConsumerLagHigh
      expr: kafka_consumer_group_lag{topic="trinetra.detections"} > 5000
      for: 2m
      severity: high
      annotations:
        summary: "Identity Resolver too slow — scale up replicas"
```

### 6.3 Drift Detection

```python
class EmbeddingDriftMonitor:
    """
    Computes rolling mean cosine similarity across all matches.
    Drift = baseline mean drops by > 0.08 over a 1h window.
    Trigger: re-calibration run or alert to model team.
    """
    BASELINE_MEAN = 0.81   # Established from validation set at deployment
    DRIFT_THRESHOLD = 0.08

    def __init__(self, window_size: int = 10000):
        self._scores = []
        self._window = window_size

    def record(self, score: float):
        self._scores.append(score)
        if len(self._scores) > self._window:
            self._scores.pop(0)

    @property
    def current_mean(self) -> float:
        return sum(self._scores) / max(1, len(self._scores))

    def is_drifting(self) -> bool:
        return (self.BASELINE_MEAN - self.current_mean) > self.DRIFT_THRESHOLD
```

---

## 7. CI/CD with GitHub Actions

> See individual workflow files in `.github/workflows/`. Summary below:

### 7.1 Why CI/CD Is Critical in CV Systems

```
Risk 1 — Silent model drift:
  A model update silently degrades embedding quality.
  Without regression tests, production drifts for days before humans notice.
  Fix: model_validation.yml runs threshold regression and drift tests on EVERY PR.

Risk 2 — Threshold regression:
  A code change accidentally modifies the cosine threshold constant.
  Fix: threshold_regression.py asserts threshold == calibrated value ± 0.01.

Risk 3 — TRT engine incompatibility:
  New GPU in production; old engine file causes silent crash.
  Fix: docker_build.yml rebuilds TRT engine on GPU runner and stores in model registry.

Risk 4 — Privacy regression:
  Code change accidentally persists raw face images.
  Fix: privacy_check.py asserts no raw image bytes are written to Postgres.
```

### 7.2 Pipeline Flows

```
Code Push → PR
    ├─ [code_quality.yml]
    │    ruff lint → mypy type check → pytest unit/
    │    On fail: PR blocked
    │
    ├─ [model_validation.yml]
    │    pytest tests/model/ (embedding consistency, threshold regression, drift)
    │    On fail: PR blocked
    │
    └─ [docker_build.yml]
         docker buildx build (multi-stage, all services)
         trivy security scan
         Push to GHCR on success

Merge to main →
    ├─ [deploy.yml — staging]
    │    docker compose pull + up (staging env)
    │    Smoke test: 60s frame ingestion + latency check
    │    On success: "Ready for production" status
    │
    └─ [deploy.yml — production]      ← Manual approval gate
         Canary: 10% traffic → new version
         Monitor 5min: error rate + latency
         Full rollout on success OR auto-rollback

Weekly schedule →
    └─ [load_test.yml]
         Locust: 8-camera burst, 600 events/sec, 10 minutes
         Assert P99 < 200ms
         Assert frames_dropped_rate < 1%
         Report to Slack
```

---

## 8. Frontend Dashboard

> Full implementation in `frontend/index.html`. Summary:

**Components:**
- **Live Camera Grid:** WebSocket-fed MJPEG stream preview per camera with overlaid detection bounding boxes
- **Identity Confidence Panel:** Real-time bar chart of Re-ID match scores per active customer
- **Journey Map:** Sankey diagram of customer paths through store zones
- **Analytics Row:** Footfall count, avg dwell time per zone, current queue length
- **Alert Feed:** Real-time scrolling alerts (VIP detected, unknown at billing, drift warning)
- **System Health Bar:** GPU utilization, Redis queue depth, Kafka lag — all live

**Technology:** Vanilla JS + WebSocket + Chart.js. No framework dependency.

**Degradation messaging:**
```javascript
ws.onclose = () => {
  document.getElementById("status-banner").innerHTML =
    "⚠️ Live feed temporarily disconnected. Data shown is from last update at " +
    lastUpdateTime + ". System is attempting reconnection...";
};
```

---

## 9. Performance & Stability Engineering

### 9.1 GPU Starvation Detection

```
Symptom: GPU_UTIL < 30% while Redis stream depth > 50 entries
Cause:   Preprocessing (CPU decode + resize) slower than TRT inference

Diagnosis flow:
  1. Check trinetra_ingestor_frame_latency_seconds P99
  2. Check trinetra_detection_latency_seconds P50
  3. If (2) << (1): preprocessing is the bottleneck

Fix: Parallelize decode with ThreadPoolExecutor(max_workers=4)
     OR: Move to server-side JPEG decode with nvJPEG (GPU)
```

### 9.2 Head-of-Line Blocking Analysis

```
HOL blocking occurs when one camera's batch inference takes 500ms
(crowded scene, tiled detection) while other cameras' frames wait.

Mitigation: Per-camera priority queue + preemptive tiling:
  - Simple scenes (< 5 detections): standard 640×640 inference
  - Dense scenes (≥ 5 detections): tiled 2×2 sub-image inference
    → more accurate but 4× slower → deprioritize in queue
  - Billing camera: always gets a dedicated inference slot (10ms reserved)
```

### 9.3 Latency vs. Throughput Profiles

```
Operating modes (tune via BATCH_SIZE env var):

┌──────────────┬────────────┬──────────┬──────────────┬────────────┐
│ Mode         │ BATCH_SIZE │ GPU UTIL │ P99 Latency  │ Max FPS    │
├──────────────┼────────────┼──────────┼──────────────┼────────────┤
│ Low-latency  │     1      │   ~30%   │    30ms      │   33fps    │
│ Balanced ✓  │     4      │   ~70%   │    75ms      │   53fps    │
│ High-thrupt  │     8      │   ~90%   │   140ms      │   57fps    │
│ Overloaded   │    16      │  ~95%    │   300ms+     │   50fps    │
└──────────────┴────────────┴──────────┴──────────────┴────────────┘

Recommendation: BATCH_SIZE=4 for production.
                BATCH_SIZE=1 for billing camera path only.
```

### 9.4 Memory Profiling Strategy

```bash
# In production: periodic Valgrind-equivalent via tracemalloc
python -c "
import tracemalloc
tracemalloc.start()
# ... run inference loop for 60s ...
snapshot = tracemalloc.take_snapshot()
for stat in snapshot.statistics('lineno')[:10]:
    print(stat)
"

# GPU memory: monitor via nvidia-smi dmon -s mu -d 5
```

---

## 10. Limitations & Risk Analysis

### 10.1 Risk Matrix

| Risk | Probability | Impact | Severity |
|---|---|---|---|
| False merge (wrong identity at billing) | **Medium** | **Critical** | ⛔ P0 |
| GPU OOM crash | Medium | Critical | ⛔ P0 |
| Embedding drift (lighting change) | High | High | 🔴 P1 |
| Identity flicker (threshold instability) | High | High | 🔴 P1 |
| Camera desynchronization >500ms | Medium | Medium | 🟡 P2 |
| Timestamp drift (NTP) | High | Medium | 🟡 P2 |
| Storage growth explosion | Medium | High | 🔴 P1 |
| Retry storms (Kafka) | Low | High | 🔴 P1 |
| Scaling limits (GPU scarcity) | Medium | Medium | 🟡 P2 |
| Privacy violation (raw image leak) | Low | Critical | ⛔ P0 |

### 10.2 Identity Many-to-Many Ambiguity

**Problem:** Two people with visually similar faces walk together through multiple cameras. Their embeddings are close in cosine space.

```
Solution — Track-level association (not frame-level):
  Frame-level: Match every detection independently. High ambiguity chance.
  Track-level: Match after ByteTrack assigns consistent track IDs across 5+ frames.
               Use HistoryConfidenceScorer with majority vote across window.

Additional guard — Companion detection:
  If two tracks are always spatially co-located (bbox IOU > 0.3 on corridor cam),
  flag as "group" — apply stricter threshold (0.85) for ID assignment.
```

### 10.3 Lighting Condition Variability

```
Problem: ArcFace embeddings shift significantly between:
  - Store open (fluorescent, warm) vs. evening (mixed/dim)
  - Camera pointing at window (backlit) vs. away from window

Mitigation:
  1. Per-camera embedding normalization: subtract camera-specific mean embedding
     (computed from gallery under that camera's lighting)
  2. Multi-enrollment: store 3 embeddings per person (morning/afternoon/evening samples)
  3. Threshold relaxation: lower threshold by 0.03 for known-difficult camera positions

Detection:
  Monitor per-camera reid_match_rate. If cam_01 drops to 0.2 while others are 0.8:
  → lighting change detected → trigger re-calibration for cam_01.
```

### 10.4 False Merges (Catastrophic)

```
Definition: Customer A's track is permanently assigned to Customer B's identity.
Consequence: B's purchase history, journey, and VIP status applied to A.
             In worst case: wrong billing charged to wrong customer.

Prevention (layered defense):
  Layer 1 — Spatiotemporal gate: rejects physically impossible transitions
  Layer 2 — History confidence scorer: requires 5-frame majority vote
  Layer 3 — Precision-first threshold (F-beta β=0.5 in calibration)
  Layer 4 — False merge detector:
```

```python
class FalseMergeDetector:
    """
    Detects when two active tracks are assigned the same customer_id simultaneously.
    This is physically impossible (one person, two cameras at different locations
    beyond travel time) → one assignment is a false merge.
    """
    def check(self, assignments: dict[int, str], registry: dict) -> list[tuple]:
        """
        assignments: {track_id → customer_id}
        Returns list of (track_id_A, track_id_B) pairs sharing an identity.
        """
        from collections import defaultdict
        reverse = defaultdict(list)
        for track_id, cust_id in assignments.items():
            reverse[cust_id].append(track_id)

        false_merges = []
        for cust_id, track_ids in reverse.items():
            if len(track_ids) > 1:
                # Same customer assigned to 2 tracks simultaneously
                # Verify via spatiotemporal gate — is this physically possible?
                # If not possible → false merge detected
                false_merges.append(tuple(track_ids))
        return false_merges
```

### 10.5 Storage Growth Explosion

```
Unconstrained growth sources:
  • JPEG frame archive: 8 cams × 15fps × 65KB = 7.8MB/sec = 28GB/hour
  • Embedding gallery: 512 × float32 × 4B × N_customers
  • Journey records: ~2KB per completed journey

Mitigation:
  • Video: rolling 72h window only (S3 lifecycle policy)
  • Raw frames: NEVER persisted (process and discard)
  • Embeddings: max 3 per customer (newest overwrites oldest if >3)
  • Journey records: archive to cold storage (S3 Glacier) after 90 days
  • Prometheus metrics: retain 7 days only

Storage budget for 1 store: ~500GB/month (video + DB + metrics)
```

### 10.6 Scaling Limits

```
Hard limits:
  GPU limit:     1 GPU per 8-10 cameras at 15fps (RTX 3080)
  Redis limit:   10,000 streams; 25GB practical DRAM limit
  Kafka limit:   100k messages/sec per broker (commodity hardware)
  Qdrant limit:  1M embeddings at <5ms search (in-memory index)
                 10M embeddings with on-disk index at ~15ms search

Bottleneck order at scale:
  1 store (8 cams): GPU is first bottleneck
  5 stores (40 cams): Network bandwidth to Redis
  50 stores: Kafka broker count (add brokers)
  500 stores: Qdrant must switch to distributed mode (Qdrant Cloud)
```

---

## 11. Production-Readiness Checklist

### Infrastructure
- [ ] Redis configured with TLS + ACL auth + MAXLEN per stream
- [ ] Kafka: 3-node cluster for HA; topics with replication-factor=2
- [ ] Qdrant: persistent volume + backup job (daily snapshot)
- [ ] PostgreSQL: WAL-based replication + daily pg_dump to S3
- [ ] All secrets in environment variables / Vault — no hardcoded credentials

### Models & Inference
- [ ] TRT engines built on target GPU SKU (CI/CD GPU runner)
- [ ] ONNX fallback models bundled alongside `.engine` files
- [ ] Re-ID threshold calibrated for each store's lighting per camera
- [ ] Gallery populated: `scripts/seed_qdrant.py` run before go-live
- [ ] FP16 verified on target GPU (accuracy regression < 0.5%)

### Code Quality
- [ ] `ruff` lint: zero violations
- [ ] `mypy --strict`: zero errors on all service code
- [ ] Unit test coverage > 80% for identity logic
- [ ] Integration tests pass with Redis + Kafka in Docker

### Security & Privacy
- [ ] No raw face images persisted anywhere in the pipeline
- [ ] All biometric embeddings encrypted at rest (Fernet + KMS)
- [ ] RTSP URLs validated against known-IP allowlist (SSRF prevention)
- [ ] Role-based access on API gateway (JWT with scopes)
- [ ] Container image: non-root user, read-only filesystem where possible
- [ ] Trivy scan: zero HIGH/CRITICAL CVEs in all images

### Observability
- [ ] All 30+ Prometheus metrics implemented and scraped
- [ ] Alert rules deployed and tested (AlertManager firing confirmed)
- [ ] Grafana dashboards showing: GPU util, queue depth, match rate, latency
- [ ] Drift monitor running with baseline established at go-live
- [ ] Structured JSON logs shipping to Loki / ELK

### Operations
- [ ] `make up` starts entire stack cleanly from zero
- [ ] `make down` + `make up` does NOT lose Qdrant gallery data
- [ ] Inference worker restart: XPENDING messages reclaimed within 60s
- [ ] Identity resolver restart: Kafka consumer lag resumes from committed offset
- [ ] Camera stream kill + restore: ingestor reconnects within 30s
- [ ] GPU OOM event: service degrades to CPU fallback (alert fires)
- [ ] Runbook: steps for re-calibration after lighting change

### Compliance
- [ ] Privacy notice deployed at store entrance
- [ ] Opt-out mechanism implemented (customer requests embedding deletion)
- [ ] Data retention policy enforced: embeddings deleted after customer request
- [ ] Audit log: every embedding access logged with timestamp + reason
- [ ] GDPR/PDPB Article 9 compliance review complete (biometric data)
