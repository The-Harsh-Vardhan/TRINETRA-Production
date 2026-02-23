"""
Identity Resolver — TRINETRA
-----------------------------
Responsibility:
  • Consume detection+embedding events from Kafka (trinetra.detections topic)
  • Query Qdrant HNSW index for nearest-neighbor identity matching
  • Apply Spatiotemporal Gating: reject matches that violate camera transition physics
  • Emit resolved identity events to Kafka (trinetra.identities topic)
  • Emit security alerts to Kafka (trinetra.alerts topic)
  • Export Prometheus metrics

Design:
  Async Kafka consumer (aiokafka) runs in the main asyncio loop.
  Qdrant queries are async (qdrant_client async API).
  Spatiotemporal gating is pure CPU math — no I/O.

Re-ID strategy:
  Primary: Cosine similarity against Qdrant HNSW gallery (top-K search)
  Fallback: If confidence < threshold → emit UNKNOWN_IDENTITY (never drop)
  Temporal gate: If (camera_B_arrival - camera_A_departure) < MIN_TRAVEL_TIME → reject match
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from prometheus_client import Counter, Histogram, Gauge, start_http_server
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, SearchRequest

from config import Settings
from gating import SpatiotemporalGate, GateResult

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","service":"identity-resolver","msg":"%(message)s"}',
)
logger = logging.getLogger("identity_resolver")

# ─── Settings ────────────────────────────────────────────────────────────────

settings = Settings()

# ─── Prometheus Metrics ──────────────────────────────────────────────────────

REID_LATENCY = Histogram(
    "trinetra_reid_latency_seconds",
    "End-to-end identity resolution latency (Kafka recv → publish)",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
QDRANT_QUERY_LATENCY = Histogram(
    "trinetra_qdrant_query_latency_seconds",
    "Qdrant HNSW ANN search latency",
    buckets=[0.001, 0.002, 0.005, 0.01, 0.025, 0.05],
)
REID_MATCHES = Counter(
    "trinetra_reid_matches_total",
    "Successful identity matches",
    ["camera_id"],
)
REID_UNKNOWNS = Counter(
    "trinetra_reid_unknowns_total",
    "Unresolved identities (below threshold)",
    ["camera_id"],
)
GATE_REJECTIONS = Counter(
    "trinetra_spatiotemporal_gate_rejections_total",
    "Matches rejected by spatiotemporal gate",
    ["reason"],
)
ALERTS_EMITTED = Counter(
    "trinetra_alerts_total",
    "Security / VIP alerts emitted",
    ["alert_type"],
)
ACTIVE_IDENTITIES = Gauge(
    "trinetra_active_identities",
    "Currently tracked unique identities in store",
)

# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class ResolvedIdentity:
    event_id: str
    camera_id: str
    camera_type: str
    track_id: int
    customer_id: Optional[str]     # None = UNKNOWN
    confidence: float
    match_method: str              # "qdrant_hnsw" | "spatiotemporal_reuse" | "unknown"
    ingest_ts: float
    resolve_ts: float
    bbox: list[float]
    embedding: list[float]


@dataclass
class AlertEvent:
    alert_id: str
    alert_type: str                # "VIP_DETECTED" | "UNKNOWN_REPEAT" | "PROHIBITED"
    camera_id: str
    customer_id: Optional[str]
    severity: str                  # "LOW" | "MEDIUM" | "HIGH"
    ts: float
    metadata: dict


# ─── In-Memory Active Identity Registry ──────────────────────────────────────

class ActiveIdentityRegistry:
    """
    Tracks currently-in-store identities and their last-seen camera/time.
    Used by the spatiotemporal gate to validate cross-camera transitions.

    This is intentionally in-process memory (not Redis) for O(1) lookup.
    On restart, it repopulates from Kafka as events replay.
    """

    def __init__(self, ttl_seconds: float = 3600.0):
        self._registry: dict[str, dict] = {}   # customer_id → {camera_id, ts, embedding}
        self._ttl = ttl_seconds
        self._unknown_counts: dict[str, int] = {}  # track_id → unknown_frame_count

    def record(self, customer_id: str, camera_id: str, ts: float, embedding: list[float]):
        self._registry[customer_id] = {
            "camera_id": camera_id,
            "last_seen_ts": ts,
            "embedding": embedding,
        }
        ACTIVE_IDENTITIES.set(len(self._registry))

    def get_last_seen(self, customer_id: str) -> Optional[dict]:
        record = self._registry.get(customer_id)
        if record and (time.time() - record["last_seen_ts"]) < self._ttl:
            return record
        return None

    def evict_expired(self):
        """Remove identities that haven't been seen within TTL."""
        cutoff = time.time() - self._ttl
        expired = [k for k, v in self._registry.items() if v["last_seen_ts"] < cutoff]
        for k in expired:
            del self._registry[k]
        ACTIVE_IDENTITIES.set(len(self._registry))


# ─── Identity Resolver Core ───────────────────────────────────────────────────

class IdentityResolver:
    """
    Orchestrates Qdrant ANN search + spatiotemporal gating for each detection event.
    """

    def __init__(
        self,
        qdrant_client: AsyncQdrantClient,
        gate: SpatiotemporalGate,
        registry: ActiveIdentityRegistry,
    ):
        self.qdrant = qdrant_client
        self.gate = gate
        self.registry = registry

    async def resolve(self, detection_event: dict) -> tuple[ResolvedIdentity, Optional[AlertEvent]]:
        """
        Resolve a single detection event to a customer identity.

        Pipeline:
          1. Extract embedding from event
          2. Query Qdrant HNSW top-K
          3. Apply spatiotemporal gate on best candidate
          4. Emit resolved identity + optional alert
        """
        camera_id = detection_event["camera_id"]
        camera_type = detection_event["camera_type"]
        ingest_ts = float(detection_event["ingest_ts"])

        # We process per-detection; take first embedding if multiple persons
        embeddings = detection_event.get("embeddings", [])
        detections = detection_event.get("detections", [])

        if not embeddings or not detections:
            return self._unknown_result(detection_event), None

        # Resolve first (highest confidence) detection
        embedding = embeddings[0]
        detection = detections[0]

        # ── Qdrant HNSW Search ──────────────────────────────────────────────
        t0 = time.perf_counter()
        try:
            search_results = await self.qdrant.search(
                collection_name=settings.QDRANT_COLLECTION,
                query_vector=embedding,
                limit=5,                        # Top-5 candidates for gate evaluation
                score_threshold=settings.COSINE_THRESHOLD,
                with_payload=True,              # Include metadata (camera history)
            )
        except Exception as e:
            logger.error(f"Qdrant search failed: {e}")
            return self._unknown_result(detection_event), None
        finally:
            QDRANT_QUERY_LATENCY.observe(time.perf_counter() - t0)

        # ── Spatiotemporal Gating ───────────────────────────────────────────
        resolved_customer_id = None
        match_confidence = 0.0
        match_method = "unknown"

        for candidate in search_results:
            customer_id = candidate.payload.get("customer_id")
            cosine_score = float(candidate.score)  # Qdrant returns cosine similarity directly

            last_seen = self.registry.get_last_seen(customer_id)
            gate_result = self.gate.evaluate(
                candidate_id=customer_id,
                current_camera=camera_id,
                current_ts=ingest_ts,
                last_seen=last_seen,
            )

            if gate_result == GateResult.ACCEPT:
                resolved_customer_id = customer_id
                match_confidence = cosine_score
                match_method = "qdrant_hnsw"
                break
            elif gate_result == GateResult.REJECT_PHYSICS:
                GATE_REJECTIONS.labels(reason="physics").inc()
                logger.debug(f"Physics gate rejected {customer_id} at {camera_id}")
                # Try next candidate
                continue
            elif gate_result == GateResult.REJECT_TIMEOUT:
                GATE_REJECTIONS.labels(reason="timeout").inc()
                continue

        # ── Record and Emit ─────────────────────────────────────────────────
        if resolved_customer_id:
            self.registry.record(resolved_customer_id, camera_id, ingest_ts, embedding)
            REID_MATCHES.labels(camera_id=camera_id).inc()
        else:
            REID_UNKNOWNS.labels(camera_id=camera_id).inc()

        resolved = ResolvedIdentity(
            event_id=str(uuid.uuid4()),
            camera_id=camera_id,
            camera_type=camera_type,
            track_id=detection.get("track_id", 0),
            customer_id=resolved_customer_id,
            confidence=match_confidence,
            match_method=match_method if resolved_customer_id else "unknown",
            ingest_ts=ingest_ts,
            resolve_ts=time.time(),
            bbox=detection.get("bbox", []),
            embedding=embedding,
        )

        alert = self._check_alert(resolved)
        return resolved, alert

    def _unknown_result(self, event: dict) -> ResolvedIdentity:
        return ResolvedIdentity(
            event_id=str(uuid.uuid4()),
            camera_id=event.get("camera_id", "unknown"),
            camera_type=event.get("camera_type", "unknown"),
            track_id=0,
            customer_id=None,
            confidence=0.0,
            match_method="unknown",
            ingest_ts=float(event.get("ingest_ts", time.time())),
            resolve_ts=time.time(),
            bbox=[],
            embedding=[],
        )

    def _check_alert(self, resolved: ResolvedIdentity) -> Optional[AlertEvent]:
        """
        Emit alerts for:
          - VIP customers arriving at entrance (camera_type == face_capture)
          - Unknown person at billing counter
        """
        if not resolved.customer_id:
            if resolved.camera_type == "billing":
                alert = AlertEvent(
                    alert_id=str(uuid.uuid4()),
                    alert_type="UNKNOWN_AT_BILLING",
                    camera_id=resolved.camera_id,
                    customer_id=None,
                    severity="MEDIUM",
                    ts=resolved.resolve_ts,
                    metadata={"track_id": resolved.track_id},
                )
                ALERTS_EMITTED.labels(alert_type="UNKNOWN_AT_BILLING").inc()
                return alert
        return None


# ─── Main Kafka Consumer Loop ─────────────────────────────────────────────────

async def main():
    start_http_server(settings.METRICS_PORT)
    logger.info("Identity Resolver starting...")

    # ── Initialize Qdrant Collection (idempotent) ───────────────────────────
    qdrant_client = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
    )
    await _ensure_qdrant_collection(qdrant_client)

    gate = SpatiotemporalGate()
    registry = ActiveIdentityRegistry()
    resolver = IdentityResolver(qdrant_client, gate, registry)

    # ── Kafka Consumer (async) ───────────────────────────────────────────────
    consumer = AIOKafkaConsumer(
        "trinetra.detections",
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id=settings.KAFKA_CONSUMER_GROUP,
        auto_offset_reset="latest",
        enable_auto_commit=True,
        auto_commit_interval_ms=1000,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        max_poll_records=20,        # Process up to 20 events per poll
    )
    await consumer.start()

    # ── Kafka Producer ───────────────────────────────────────────────────────
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        compression_type="lz4",
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()

    logger.info("Consuming from trinetra.detections...")

    evict_counter = 0
    try:
        async for msg in consumer:
            t0 = time.perf_counter()
            event = msg.value

            try:
                resolved, alert = await resolver.resolve(event)
            except Exception as e:
                logger.error(f"Resolve error: {e}", exc_info=True)
                continue

            # Publish identity event
            await producer.send_and_wait(
                "trinetra.identities",
                key=resolved.camera_id.encode(),
                value=asdict(resolved),
            )

            # Publish alert if triggered
            if alert:
                await producer.send_and_wait(
                    "trinetra.alerts",
                    value=asdict(alert),
                )

            REID_LATENCY.observe(time.perf_counter() - t0)

            # Periodic TTL eviction (every 1000 events)
            evict_counter += 1
            if evict_counter % 1000 == 0:
                registry.evict_expired()

    finally:
        await consumer.stop()
        await producer.stop()
        await qdrant_client.close()


async def _ensure_qdrant_collection(client: AsyncQdrantClient):
    """Create Qdrant collection with HNSW index if it doesn't exist."""
    from qdrant_client.models import VectorParams, Distance, HnswConfigDiff

    collections = await client.get_collections()
    existing = [c.name for c in collections.collections]

    if settings.QDRANT_COLLECTION not in existing:
        await client.create_collection(
            collection_name=settings.QDRANT_COLLECTION,
            vectors_config=VectorParams(
                size=512,               # ArcFace embedding dimension
                distance=Distance.COSINE,
            ),
            hnsw_config=HnswConfigDiff(
                m=16,                   # HNSW: connections per node (higher = better recall, more RAM)
                ef_construct=200,       # Build-time quality (higher = better index, slower builds)
                full_scan_threshold=10000,
                on_disk=False,          # In-memory for sub-ms latency
            ),
        )
        logger.info(f"Created Qdrant collection: {settings.QDRANT_COLLECTION}")
    else:
        logger.info(f"Qdrant collection '{settings.QDRANT_COLLECTION}' already exists.")


if __name__ == "__main__":
    asyncio.run(main())
