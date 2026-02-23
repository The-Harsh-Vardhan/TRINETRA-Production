# TRINETRA — Architecture Overview

> **Status:** Production Engineering Reference  
> **Maintainer:** Principal Engineer, CV & AI  
> **Last Updated:** February 2026

---

## System Overview

TRINETRA is a distributed, event-driven multi-camera computer vision platform for real-time smart retail analytics. It converts raw RTSP camera streams into structured customer identity, behavioral, and transactional insights.

---

## High-Level Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        PHYSICAL CAMERA LAYER                                 │
│   [Entrance Cam] [Face Cam] [Tracking Cam] [Billing Cam] [Vehicle Cam]      │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │ RTSP (TCP)
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                    SERVICE: STREAM INGESTOR                                  │
│  • Per-camera asyncio task  • Adaptive frame sampler  • JPEG compression     │
│  • Reconnect with exp. backoff  • Prometheus metrics                         │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │ Redis XADD (frames:{camera_id})  MAXLEN~100
                              ▼
                    ┌─────────────────┐
                    │   REDIS STREAMS │  ◄─ Bounded frame buffer
                    │ (Frame Buffer)  │     Backpressure via tail-drop
                    └────────┬────────┘
                             │ XREADGROUP (consumer group)
                             ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                    SERVICE: INFERENCE WORKER (GPU)                           │
│  • Micro-batch accumulator (N=4, timeout=20ms)                               │
│  • YOLOv8-m TRT: person detection                                            │
│  • ArcFace-R50 TRT: 512-dim face embedding                                   │
│  • Kafka producer (trinetra.detections, LZ4)                                 │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │ Kafka Produce → trinetra.detections (8 partitions)
                              ▼
                 ┌────────────────────────┐
                 │   KAFKA (Event Spine)  │  ◄─ 3 topics:
                 │  (Cross-Service State) │     .detections / .identities / .alerts
                 └──────────┬─────────────┘
                            │ Kafka Consume (group: identity-resolver-group)
                            ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                 SERVICE: IDENTITY RESOLVER (CPU)                             │
│  • Async Kafka consumer (aiokafka)                                           │
│  • Qdrant HNSW: cosine ANN search (top-K, threshold=0.72)                   │
│  • Spatiotemporal Gate: physics-based transition validation                  │
│  • Active Identity Registry (in-memory, TTL-evicted)                        │
│  • Alert emission to trinetra.alerts                                         │
└────────┬──────────────────────────────────────────┬───────────────────────────┘
         │ Kafka Produce → trinetra.identities        │ Kafka Produce → trinetra.alerts
         ▼                                            ▼
  ┌────────────────┐                        ┌──────────────────┐
  │  QDRANT        │                        │  Dashboard /     │
  │  Vector DB     │                        │  Alert Service   │
  │  (HNSW index)  │                        │  (downstream)    │
  └────────────────┘                        └──────────────────┘

                    ┌─────────────────────────────────┐
                    │  OBSERVABILITY LAYER             │
                    │  Prometheus ← scrapes all ports  │
                    │  Grafana ← queries Prometheus    │
                    └─────────────────────────────────┘
```

---

## Technology Decision Summary

| Technology | Role | Why Chosen |
|---|---|---|
| **Redis Streams** | Frame ingestion buffer | Ordered, bounded, consumer-group aware. O(1) MAXLEN trim. Survives worker crash (XPENDING replay). |
| **Kafka** | Cross-service event backbone | Durable, partitioned, replayable. Decouples inference from identity resolution. Scales to 100k events/sec. |
| **Qdrant** | Face embedding vector search | HNSW index, native cosine distance, filterable payloads, gRPC API. Sub-5ms ANN search on millions of embeddings. |
| **TensorRT** | GPU inference backend | 3–8× faster than PyTorch for YOLO + ArcFace. FP16 halves VRAM. Engine locked to GPU SKU — rebuilt in CI/CD. |
| **ONNX Runtime** | TRT fallback | CPU inference compatibility for non-GPU dev environments. |
| **Prometheus + Grafana** | Observability | De facto standard. Histogram-based latency tracking. Alert rules on P99 latency, drop rate, drift. |
| **FastAPI + asyncio** | Service framework | Non-blocking I/O for RTSP + Redis operations. Minimal overhead per-frame. |

---

## Data Flow Summary

```
Camera Frame (raw JPEG ~500KB)
    ↓ resize to 640×640 (~60KB JPEG)
Redis Stream entry (~65KB per frame)
    ↓ YOLOv8 detection
Detection bboxes + ArcFace 512-dim float32 embedding (~2KB JSON)
    ↓ Kafka message
Qdrant ANN search result → customer_id
    ↓ Spatiotemporal gate
Resolved identity event (JSON, ~1KB)
    ↓ Kafka → downstream analytics
```

---

## Service Dependency Map

```
stream-ingestor → [Redis]
inference-worker → [Redis (consumer), Kafka (producer)]
identity-resolver → [Kafka (consumer), Qdrant, Kafka (producer)]
grafana → [Prometheus]
prometheus → [stream-ingestor:8001, inference-worker:8002, identity-resolver:8003]
```

Start order enforced by Docker Compose `depends_on` with `condition: service_healthy`.

---

## Failure Isolation Model

Each service is independently restartable. Crash domains:

| Service Crash | Impact | Recovery |
|---|---|---|
| stream-ingestor | New frames stop; Redis drains | Restart; reconnects all cameras |
| inference-worker | Frames accumulate in Redis (up to MAXLEN, then dropped) | Restart; claims XPENDING messages |
| identity-resolver | Detections accumulate in Kafka | Restart; replays from committed offset |
| Redis | Stream buffer lost | Restart; ingestor re-populates immediately |
| Kafka | Events lost until recovered | Leader election; consumers reconnect |
| Qdrant | Re-ID falls back to UNKNOWN | Restart; index persisted on disk |
