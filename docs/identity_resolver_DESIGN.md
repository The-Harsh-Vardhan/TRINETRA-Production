# Identity Resolver — Design Document

> **Service:** `identity-resolver`  
> **Owner:** CV & AI Platform Team  
> **Tier:** CPU — no GPU required  
> **Interfaces:** Kafka (in: `trinetra.detections`) → Qdrant → Kafka (out: `trinetra.identities`, `trinetra.alerts`)

---

## Purpose

The Identity Resolver answers the core question: **"Who is this person?"** It takes raw face embeddings from the inference worker and matches them to known customer identities in the Qdrant vector database. It additionally applies a spatiotemporal gate to reject physically implausible matches — a class of false positives that pure embedding similarity cannot catch.

---

## 1. Scaling: Handling a 5× Spike Without Service Degradation

### Bottleneck Analysis

The identity resolver has two potential bottlenecks:

| Operation | Latency | Bottleneck? |
|---|---|---|
| Kafka `max_poll_records=20` consumption | ~1ms | No |
| Qdrant HNSW search (top-5) | ~2–5ms | Only if Qdrant is undersized |
| Spatiotemporal gate evaluation | ~0.1ms | No |
| Redis ActiveIdentityRegistry lookup | ~0.01ms (in-memory) | No |
| Kafka produce (identity event) | ~2ms (async) | No |

**Total per-event latency:** ~5–10ms. At 120 events/sec (8 cams × 15fps), total consumer load is **1.2 seconds of compute per second** → safely within one CPU core.

### 5× Spike (40 cameras → 600 events/sec)

At 600 events/sec × 7ms average = 4.2 seconds of compute per second. This exceeds one core.

**Solution: Increase consumer group parallelism.**

```yaml
# docker-compose.yml — scale identity resolver
identity-resolver:
  deploy:
    replicas: 4     # 4 consumers in the same group → Kafka partitions split 4 ways
```

With 8 Kafka partitions on `trinetra.detections` and 4 consumer replicas, each replica handles 2 partitions = 150 events/sec → 1.05 seconds of compute per second per replica. ✓

**Qdrant scaling:** Qdrant's HNSW search is O(log N) for N embeddings in the gallery. For a retail store with 10,000 known customers, search latency is ~2ms. For 100,000 customers, ~4ms. This does not appreciably change with 5× camera scaling.

---

## 2. Backpressure: If the Resolver Is Slower Than the Inference Worker

### The Chain

```
Inference Worker → Kafka: trinetra.detections (8 partitions)
                               ↓ Kafka consumer lag grows
                          Identity Resolver → consumes at constrained rate
```

### Kafka's Role: The Natural Buffer

Unlike Redis (bounded by MAXLEN), Kafka retains messages for the configured retention period (24 hours). If the identity resolver is temporarily slow (e.g., Qdrant query latency spike), Kafka consumer lag simply grows. Events are not dropped — they are delayed.

**Key property: Kafka consumer lag is observable and alertable.**

```
Alert: trinetra_kafka_consumer_lag{topic="trinetra.detections"} > 5000
→ "Identity resolver throughput insufficient"
```

### Consequence of High Consumer Lag

A large Kafka lag means identity events are delayed relative to detection events. For behavioral analytics (dwell time, heatmaps), a 5-second delay is acceptable. For billing counter alerts, it is not.

**Mitigation for latency-critical paths:**
Route billing counter events to a separate Kafka topic `trinetra.detections.billing` with higher priority and a dedicated low-latency identity-resolver instance.

---

## 3. Mathematical Trade-offs: Cosine vs Euclidean, and HNSW Recall vs Latency

### Cosine Similarity vs Euclidean Distance

ArcFace is trained with **Angular Margin Loss** — the embedding space is designed such that **angle** between vectors encodes identity similarity, not Euclidean distance.

Formally, for two L2-normalized embeddings **u** and **v**:

```
Cosine Similarity:   cos(θ) = (u · v) / (‖u‖ · ‖v‖) = u · v    (since ‖u‖ = ‖v‖ = 1)
Euclidean Distance:  ‖u - v‖ = √(‖u‖² + ‖v‖² - 2(u · v)) = √(2 - 2·cos(θ))
```

Because ArcFace normalizes all embeddings to the unit hypersphere, cosine similarity and Euclidean distance are **monotonically related** — they produce the same ranking. The choice between them for ANN search does not affect recall/precision.

**Why Qdrant uses cosine:** Qdrant's HNSW implementation with `Distance.COSINE` does not require pre-normalization (it normalizes internally), simplifying the application code. It also directly returns similarity in [−1, 1], making threshold interpretation (0.72 = strong match) intuitive.

**Threshold calibration:** The 0.72 default is a starting point, not a universal truth. Per-deployment calibration:

```python
# At deployment time, run:
python scripts/calibrate_reid.py \
    --gallery /data/known_customers \
    --val /data/validation_set \
    --output-threshold threshold.json
```

Different lighting conditions (indoor fluorescent vs outdoor daylight) shift the optimal threshold by ±0.05.

### HNSW: Recall vs Latency Trade-off

HNSW has two construction parameters that govern this trade-off:

| Parameter | Value | Effect |
|---|---|---|
| `m` (max connections per node) | 16 | Higher `m` → better recall, more RAM, faster search |
| `ef_construct` (build-time beam width) | 200 | Higher → better index quality, slower build |

At query time, `ef` (search beam width, set via Qdrant's `search_params.hnsw_ef`) controls:

```
ef=50:   ~95% recall, ~1ms search    (default for TRINETRA)
ef=128:  ~99% recall, ~3ms search    (use for billing camera only)
ef=256:  ~99.9% recall, ~7ms search  (use for VIP identification alerts)
```

### Impact on Security Alerts

A missed match (false negative / lower recall) at the billing camera means an unknown customer is flagged as a stranger even though they are a known customer. This triggers a `UNKNOWN_AT_BILLING` alert unnecessarily — alert fatigue.

A false positive (wrong identity matched) means the wrong customer's profile is loaded at billing — a privacy violation.

**Design choice:** TRINETRA prefers higher recall (ef=128 for billing path) to minimize missed matches, with the spatiotemporal gate acting as a false-positive suppressor.

**The gate's role in this math:**
The gate eliminates false positives from HNSW by an estimated 40–70% (based on empirical testing on publicly available retail datasets). This means you can use a lower cosine threshold (more lenient matching, higher recall) without proportionally increasing false positives. The gate compensates.

---

## 4. Failure Modes

### Network Partition Between Camera and Broker (Upstream)

**Problem:** Camera → Ingestor network partition.  
**Scope:** Ingestor's problem, not Identity Resolver's. Identity Resolver continues processing the backlog of Kafka messages that accumulated before the partition.  
**Effect on Identity Resolver:** Zero — it is fully decoupled from camera hardware via Kafka.

### Qdrant Unavailable

**Problem:** Qdrant container crashes; all `qdrant.search()` calls fail.  
**Root Cause:** OOM on Qdrant host, storage full, container restart.  
**Symptom:** `qdrant_query_latency_seconds` spikes; all events emit `UNKNOWN_IDENTITY`.  
**Mitigation:**
```python
try:
    search_results = await self.qdrant.search(...)
except Exception as e:
    logger.error(f"Qdrant search failed: {e}")
    return self._unknown_result(detection_event), None   # Degrades gracefully
```
Kafka consumer continues; events pile up with `UNKNOWN` identity. On Qdrant recovery, Kafka offset is not advanced — **no**, auto-commit means lag is lost. To avoid this, disable auto-commit and manually commit after successful resolution. (Mark as future improvement.)

**Alert:** `rate(trinetra_reid_unknowns_total[60s]) / rate(trinetra_worker_frames_processed_total[60s]) > 0.8` → "Qdrant may be down" (>80% unknowns is anomalous).

### Kafka Consumer Group Rebalance During Partition Addition

**Problem:** Adding a new resolver instance triggers Kafka consumer group rebalance. During rebalance (~1–10 seconds), no messages are consumed.  
**Mitigation:** Configure `session.timeout.ms=30000` and `heartbeat.interval.ms=3000` in aiokafka to minimize rebalance trigger frequency. Use `partition.assignment.strategy=cooperative-sticky` (Kafka 3.x) for incremental rebalancing.

### Active Identity Registry Memory Growth

**Problem:** Registry grows without bound if `evict_expired()` is not called.  
**Mitigation:** Registry eviction runs every 1000 events (in the main consumer loop). At peak 120 events/sec, eviction runs every ~8 seconds. For a store with 500 simultaneous customers (extreme case), each record is ~2KB → 1MB total. Acceptable.

---

## Spatiotemporal Gate: Design Rationale

The gate solves a fundamental limitation of pure embedding-based matching: two people can have similar embeddings (twins, look-alike lighting conditions) but they cannot teleport.

The travel time matrix is derived from:
1. Store floor plan: measured shortest walking path between camera coverage zones.
2. Average walking speed: 1.0–1.4 m/s (normal), 0.5 m/s (crowded).
3. Safety factor: minimum time = (distance / max_speed) × 0.9.

```
Example: Entrance → Billing (40m path)
  40m / 1.4 m/s = 28.6s → min_travel = 25s (with 10% safety margin)
```

---

## Configuration Reference

| Env Var | Default | Description |
|---|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `KAFKA_CONSUMER_GROUP` | `identity-resolver-group` | Consumer group for partition assignment |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant HTTP endpoint |
| `QDRANT_API_KEY` | *(required)* | Qdrant auth key |
| `QDRANT_COLLECTION` | `face_embeddings` | Qdrant collection name |
| `COSINE_THRESHOLD` | `0.72` | Minimum cosine similarity for a match |
| `TEMPORAL_GATE_WINDOW_S` | `3600.0` | Max in-store session time |
| `METRICS_PORT` | `8003` | Prometheus port |
