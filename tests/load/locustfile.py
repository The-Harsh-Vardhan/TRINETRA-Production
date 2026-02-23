"""
Locust Load Test — TRINETRA Multi-Camera Burst Simulation
----------------------------------------------------------
Simulates 8 concurrent camera feeds pushing frames into Redis at 15fps.
Validates pipeline throughput, frame-drop rate, and Redis backpressure.

Run:
    locust -f locustfile.py --headless --users 8 --spawn-rate 1 --run-time 600s
"""

import random
import time
import io
import numpy as np
import cv2
from locust import User, task, between, events
from locust.runners import MasterRunner

import redis

# ─── Synthetic frame generator ────────────────────────────────────────────────

CAMERA_TYPES = ["entrance", "face_capture", "tracking", "billing", "vehicle"]


def generate_synthetic_frame(width: int = 640, height: int = 640) -> bytes:
    """
    Generate a synthetic noise frame that passes frame validation.
    (std_dev > 5, mean between 2 and 253)
    """
    frame = np.random.randint(30, 220, (height, width, 3), dtype=np.uint8)
    # Add some structure (random circles) to simulate persons
    for _ in range(random.randint(0, 5)):
        cx = random.randint(50, width - 50)
        cy = random.randint(50, height - 50)
        cv2.circle(frame, (cx, cy), random.randint(20, 60), (200, 150, 100), -1)
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


# ─── Redis Camera User ────────────────────────────────────────────────────────

class CameraFeedUser(User):
    """
    Simulates a single RTSP camera's ingestion load.
    Each virtual user = 1 camera, pushes at TARGET_FPS rate.
    """
    TARGET_FPS = 15
    FRAME_INTERVAL_S = 1.0 / TARGET_FPS   # ~66ms between frames
    MAXLEN = 100

    abstract = False
    wait_time = between(0.06, 0.07)   # ~66ms ± 5ms (matches 15fps)

    def on_start(self):
        self.redis_client = redis.Redis(
            host="localhost", port=6379,
            password="trinetra_redis_secret",
            decode_responses=False,
        )
        self.camera_id = f"cam_load_{self.user_id:02d}"
        self.camera_type = random.choice(CAMERA_TYPES)
        self.frame_count = 0
        self.dropped_count = 0

    @task
    def push_frame(self):
        """Push one synthetic frame to Redis Stream."""
        frame_bytes = generate_synthetic_frame()
        t0 = time.perf_counter()

        try:
            # Check current stream length for backpressure observation
            stream_len = self.redis_client.xlen(f"frames:{self.camera_id}")

            msg_id = self.redis_client.xadd(
                f"frames:{self.camera_id}",
                {
                    "camera_id": self.camera_id,
                    "camera_type": self.camera_type,
                    "ingest_ts": str(time.time()),
                    "frame": frame_bytes,
                },
                maxlen=self.MAXLEN,
                approximate=True,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.frame_count += 1

            # Report to Locust metrics
            events.request.fire(
                request_type="REDIS_XADD",
                name=f"frames:{self.camera_type}",
                response_time=elapsed_ms,
                response_length=len(frame_bytes),
                exception=None,
            )

        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.dropped_count += 1
            events.request.fire(
                request_type="REDIS_XADD",
                name=f"frames:{self.camera_type}",
                response_time=elapsed_ms,
                response_length=0,
                exception=e,
            )

    def on_stop(self):
        drop_rate = self.dropped_count / max(1, self.frame_count)
        print(
            f"[{self.camera_id}] frames={self.frame_count} "
            f"dropped={self.dropped_count} ({drop_rate:.1%})"
        )


# ─── Assertions (printed at end of test) ─────────────────────────────────────

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats.total
    p99_ms = stats.get_response_time_percentile(0.99)
    failure_rate = stats.num_failures / max(1, stats.num_requests)

    print("\n─── LOAD TEST RESULTS ───────────────────────────────")
    print(f"  Total requests:    {stats.num_requests}")
    print(f"  Failures:          {stats.num_failures}")
    print(f"  Failure rate:      {failure_rate:.2%}")
    print(f"  P99 latency:       {p99_ms:.1f}ms")
    print(f"  RPS:               {stats.current_rps:.1f}")

    # Assert SLAs
    assert p99_ms < 200, f"FAIL: P99 {p99_ms:.0f}ms > 200ms SLA"
    assert failure_rate < 0.01, f"FAIL: Failure rate {failure_rate:.2%} > 1%"
    print("  ✓ All SLAs passed")
    print("─────────────────────────────────────────────────────\n")
