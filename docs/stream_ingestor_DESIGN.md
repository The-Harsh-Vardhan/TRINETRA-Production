# Stream Ingestor — Design Document

> **Service:** `stream-ingestor`  
> **Owner:** CV & AI Platform Team  
> **Tier:** Infrastructure (no GPU required)  
> **Interfaces:** RTSP (in) → Redis Streams (out), HTTP /health, /metrics

---

## Purpose

The Stream Ingestor is the gateway between physical RTSP cameras and the processing pipeline. It handles all I/O instability (camera disconnects, network jitter, varying frame rates) and presents a clean, bounded, ordered frame stream to downstream consumers.

**It is the only service that touches RTSP.** This isolation means RTSP reconnect logic, codec handling, and frame decoding bugs never affect inference.

---

## 1. Scaling: Handling a 5× Camera Feed Spike

### Current Design

Each camera runs two asyncio tasks: a blocking RTSP reader (in a thread executor) and an async Redis publisher. These tasks are extremely lightweight — mostly I/O wait.

### 5× Spike Scenario

Scaling from 8 → 40 cameras:

| Resource | 8 Cameras | 40 Cameras | Impact |
|---|---|---|---|
| CPU | ~5% (decode + resize) | ~25% | Acceptable on 4-core node |
| RAM | ~200MB (frame buffers) | ~1GB | Within Docker limit |
| Redis connections | 8 | 40 | Redis supports 10,000+ connections |
| Network bandwidth | ~80 Mbps (15fps × 8 × ~640KB avg) | ~400 Mbps | Requires Gigabit LAN |

### Mitigation: Horizontal Sharding

If one ingestor node is saturated at CPU, deploy multiple ingestor instances:

```
Camera Group A (cams 1-20) → Ingestor Instance 1
Camera Group B (cams 21-40) → Ingestor Instance 2
                                ↓ (both publish to same Redis)
                        Inference Worker (shared consumer group)
```

The ingestor is stateless per-restart — no shared state needed between instances. Cameras are partitioned via the `cameras.yaml` config, one config per instance.

**Key design decision:** Adaptive frame sampling (`AdaptiveFrameSampler`) automatically reduces the injection rate when Redis stream fill exceeds 80%. This prevents memory exhaustion without requiring operator intervention on demand spikes.

---

## 2. Backpressure: What Happens If the Inference Worker Is Slower Than the Ingestor?

### Problem

At 8 cameras × 15fps = 120 frames/second input. If inference throughput is only 60fps (e.g., GPU thermal throttle), frames accumulate.

### Layers of Defense

```
Layer 1 — AdaptiveFrameSampler:
  Reads Redis stream fill % before each publish.
  If fill > 80%: increases frame skip interval (drops 1-in-2, 1-in-3, etc.)
  Effect: Reduces input rate to match consumer capacity. Automatic.

Layer 2 — Redis MAXLEN (APPROX):
  Each stream capped at 100 entries.
  XADD with MAXLEN ~ trims oldest entries (O(1) amortized).
  Effect: Bounds memory to ~6.5MB per camera (100 × 65KB JPEG).
  Oldest frames are silently dropped — acceptable; recent frames matter more.

Layer 3 — In-Process Queue Full Handler:
  asyncio.Queue bounded to FRAME_BUFFER_MAXLEN.
  On full: explicitly dequeue oldest, enqueue newest (tail-drop with FIFO eviction).
  FRAMES_DROPPED counter incremented — visible in Prometheus.
```

### Observability

Alert rule: `rate(trinetra_ingestor_frames_dropped_total[60s]) > 5` → page on-call.

This signals the inference worker needs horizontal scaling or frame rate reduction.

### What Is Never Dropped

Billing counter camera frames are **not** subject to aggressive dropping. In a future enhancement, a priority queue can ensure billing camera frames always have headroom in the Redis stream.

---

## 3. Mathematical Trade-offs: Frame Sampling Decisions

### Optical Flow Motion Score

```python
motion = mean(|flow_x|² + |flow_y|²)^0.5
```

This is the mean Farneback optical flow magnitude across all pixels. Range: [0, ∞).

- Static scene (empty store): ~0.1–0.5 → skip more frames (increase interval)
- Normal movement: ~1.0–3.0 → baseline sampling
- High activity (crowd, rush hour): ~5.0+ → skip fewer frames

**Trade-off:** Computing Farneback on every frame adds ~2ms CPU overhead per camera. This is acceptable vs. the ~50ms GPU inference saved by skipping a non-informative frame.

---

## 4. Failure Modes

### Camera Stream Failure (RTSP disconnect)

**Problem:** `cap.read()` returns `(False, None)`.  
**Root Cause:** Network drop, camera reboot, PSU failure.  
**Symptom:** `STREAM_RECONNECTS` counter increases; `FRAMES_INGESTED` plateaus.  
**Mitigation:**
- Dedicated RTSP reader thread detects failure within one frame timeout.
- Exponential backoff reconnect: 1s → 2s → 4s → … → 30s ceiling.
- Tracker state (held by inference worker) is preserved across ingestor reconnects.
- Redis stream retains existing frames during the gap — no data loss on short outages (<60s).

**Alert:** `rate(trinetra_ingestor_reconnects_total[5m]) > 3` for any camera → camera hardware alert.

### Network Partition (Camera → Ingestor)

**Problem:** Camera is running but packets can't reach the ingestor host.  
**Symptom:** Same as stream failure — RTSP read returns False after FFmpeg timeout.  
**Mitigation:** Same reconnect logic. FFmpeg TCP timeout triggers within 2–5 seconds.  
**Alert:** If reconnect attempts fail for >60s → emit `CAMERA_OFFLINE` event to Kafka alerts topic.

### Redis Unavailable

**Problem:** Redis container crashes or is unreachable.  
**Symptom:** `redis.exceptions.ConnectionError` on `xadd`.  
**Mitigation:** Redis client retries with backoff (configured via `socket_keepalive` and `retry_on_timeout`). Frames are dropped during Redis unavailability — no local buffer overflow in the ingestor (by design; storing frames locally would OOM).  
**Recovery:** On Redis restart, ingestor resumes publishing immediately.

---

## Configuration Reference

| Env Var | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `FRAME_BUFFER_MAXLEN` | `100` | Max Redis stream entries per camera |
| `TARGET_FPS` | `15` | Default target inference FPS (override per camera in cameras.yaml) |
| `CAMERA_CONFIGS` | `/etc/trinetra/cameras.yaml` | Camera configuration file path |
| `METRICS_PORT` | `8001` | Prometheus exposition port |
