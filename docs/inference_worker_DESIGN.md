# Inference Worker — Design Document

> **Service:** `inference-worker`  
> **Owner:** CV & AI Platform Team  
> **Tier:** GPU — requires NVIDIA GPU with TensorRT  
> **Interfaces:** Redis Streams (in) → Kafka (out), /metrics

---

## Purpose

The Inference Worker is the computational core of TRINETRA. It transforms raw compressed frames into structured detection events: bounding boxes, class confidences, and 512-dimensional face embeddings. All GPU compute lives here.

---

## 1. Scaling: Handling a 5× Camera Spike Without Crashing the GPU

### GPU Memory Budget (RTX 3080, 10GB VRAM)

| Component | VRAM (FP16) |
|---|---|
| YOLOv8-m TRT engine | ~900 MB |
| ArcFace-R50 TRT engine | ~400 MB |
| Frame batch tensor (B=4, 640×640×3) | ~120 MB |
| Face crop batch tensor (B=16, 112×112×3) | ~8 MB |
| CUDA workspace + TRT optimizer | ~1000 MB |
| **Total allocated** | **~2.4 GB** |
| **Available headroom** | **~7.6 GB** |

With adequate VRAM headroom, a 5× spike (from 8 to 40 cameras) does **not** increase GPU memory usage — VRAM consumption is determined by batch size, not stream count. The batch size is fixed at N=4.

### What Does Increase

- **Redis consumer throughput:** More cameras → more messages/second. Mitigated by `XREADGROUP` with `count=BATCH_SIZE` — the worker pulls exactly as many frames as it can process.
- **CPU preprocessing:** `_preprocess_batch` is CPU-bound. If preprocessing becomes the bottleneck, parallelize with `concurrent.futures.ThreadPoolExecutor`.
- **Kafka publish rate:** More detections → more Kafka messages. Kafka can sustain 100k+ messages/sec on commodity hardware.

### Horizontal Scaling

Multiple inference-worker instances join the same Redis consumer group (`inference-workers`). Redis automatically distributes stream entries across consumers. Each worker independently owns its GPU context.

```
stream-ingestor → Redis: frames:cam_01, frames:cam_02, ...
                          ↓ XREADGROUP (round-robin across)
        Worker-1 (GPU 0)   Worker-2 (GPU 1)
```

**Critical constraint:** CUDA contexts cannot be forked — processes must be spawned (`mp.get_context("spawn")`), not forked. The current implementation uses a single-process loop, safe from this issue.

---

## 2. Backpressure: What Happens If the Worker Is Slower Than the Ingestor?

### The Backpressure Chain

```
Camera (30fps) → [Ingestor: adaptive sampling → 15fps] → Redis (MAXLEN=100)
                                                                    ↓
                                                         Worker pulls at ~60fps effective
                                                         (4-frame batch @ ~67ms/batch)
```

At 8 cameras × 15fps = 120 frames/sec input, one worker at ~60fps effective throughput means frames accumulate. The resolution:

**Layer 1 — Micro-batch timeout (20ms)**

The `MicroBatchAccumulator` flushes after 20ms even if the batch isn't full. This bounds worst-case latency to `batch_fill_time + inference_time`.

```
At 8 cameras × 15fps: frame arrives every ~8ms.
Full batch of 4 fills in ~32ms → timeout at 20ms wins → flush partial batch.
Effective throughput: 4 frames per ~70ms = ~57fps. ✓
```

**Layer 2 — Redis MAXLEN (backpressure source)**

If the worker is consistently slower than input, Redis MAXLEN acts as the circuit breaker. Oldest frames are evicted. This is acceptable; TRINETRA always prefers recency over completeness.

**Layer 3 — XPENDING Reclaim on Restart**

If a worker crashes mid-batch, the messages it ACKed are free. The messages it read but didn't ACK remain in XPENDING. On next startup, `xautoclaim` reclaims those messages and reprocesses them. No frame is permanently lost.

### Alert Rule

```
# Alert: Worker throughput below input rate
rate(trinetra_ingestor_frames_dropped_total[5m]) > 2
AND
rate(trinetra_worker_frames_processed_total[5m]) < 50
→ "Inference worker throughput insufficient — scale horizontally"
```

---

## 3. Mathematical Trade-offs: Batch Sizing and FP16

### Micro-Batch Size (N)

Throughput is not linear with batch size due to GPU warp scheduling:

| Batch N | GPU Utilization | Latency (P99) | Effective FPS |
|---|---|---|---|
| 1 | ~30% | 25ms | ~40fps |
| 4 | ~70% | 70ms | ~57fps |
| 8 | ~90% | 140ms | ~57fps |

N=4 is the recommended sweet spot: 70% GPU utilization with bounded latency. N=8 does not improve throughput but doubles latency.

### FP16 vs FP32

ArcFace was validated to lose <0.3% verification accuracy when quantized to FP16 (measured on LFW benchmark). The benefit: 2× throughput and 2× VRAM reduction. FP16 is always enabled for both YOLO and ArcFace engines in TRINETRA.

### Confidence Threshold (0.35)

Person detection threshold is set deliberately low (0.35 vs default 0.5) because:
- CCTV footage is often low-contrast, backlit, or partially occluded.
- False negatives (missed persons) cause track IDs to be uninitialized — harder to recover than a false positive.
- NMS (IoU=0.45) eliminates duplicate detections from low-threshold candidates.

Tune per-deployment based on false positive rate from monitoring.

---

## 4. Failure Modes

### GPU OOM (Out of Memory)

**Problem:** Inference fails with `torch.cuda.OutOfMemoryError` or TensorRT CUDA error.  
**Root Cause:** Unexpectedly large face crop batch (crowded scene), VRAM fragmentation.  
**Mitigation:**
- `ArcFaceEmbedder.embed_batch()` sub-batches to MAX_SUB_BATCH=16.
- `_process_batch()` catches exceptions → returns `[]` embeddings for failed frames.
- Message is still ACKed (not replayed into another OOM cycle).
- Alert: `trinetra_gpu_vram_used_mb > 9000` → pre-OOM warning.

### TensorRT Engine Incompatibility

**Problem:** `.engine` file compiled on different GPU → `RuntimeError` on load.  
**Root Cause:** TRT engines are GPU-architecture-specific (SM version mismatch).  
**Mitigation:** Engine export is a CI/CD step, tagged with GPU SKU. `YOLO_ENGINE_PATH` is set per-deployment. ONNX `.onnx` fallback model is always bundled alongside `.engine`.

### Kafka Producer Failure

**Problem:** Kafka is unreachable; `kafka_producer.produce()` fails.  
**Root Cause:** Network partition, Kafka restart, topic deletion.  
**Mitigation:**
- Confluent Kafka producer retries=5 with 100ms backoff.
- `KAFKA_PUBLISH_ERRORS` counter tracked; alert on persistent failure.
- Frames are still ACKed from Redis (to prevent OOM loop) — accepted data loss during Kafka outage. Kafka availability is an SLA dependency.

### Corrupted Frame in Redis

**Problem:** `cv2.imdecode` returns `None` — frame bytes are corrupt.  
**Root Cause:** Partial write to Redis during ingestor crash, or JPEG codec error.  
**Mitigation:** `if frame is None: _validate_and_ack(msg, redis_client); continue` — skip and ACK immediately. `FRAMES_DROPPED` counter incremented.

---

## Configuration Reference

| Env Var | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `YOLO_ENGINE_PATH` | `/models/yolov8m.engine` | TRT engine for detection |
| `ARCFACE_ENGINE_PATH` | `/models/arcface_r50.engine` | TRT engine for embeddings |
| `BATCH_SIZE` | `4` | Micro-batch size |
| `BATCH_TIMEOUT_MS` | `20.0` | Max batch wait time |
| `METRICS_PORT` | `8002` | Prometheus port |
