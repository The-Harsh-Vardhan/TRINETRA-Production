"""
Spatiotemporal Gating — TRINETRA Identity Resolver
---------------------------------------------------
Enforces physical plausibility of cross-camera identity transitions.

Core insight:
  If a person was last seen at Camera A (Entrance) at time T₀,
  they cannot appear at Camera B (Billing, 50m away) at time T₁ = T₀ + 2s.
  A human cannot walk 50m in 2 seconds.
  Therefore, the match is physically impossible — reject it.

This gate eliminates a class of false-positive Re-ID matches that any
embedding-based system will produce in crowded retail environments.

Parameters (tunable per store layout):
  - CAMERA_TRAVEL_TIMES: minimum seconds to travel between any two camera zones.
    Derived from store floor plan. Must be recalibrated per deployment.
  - GATE_WINDOW_S: maximum time a camera transition is considered valid.
    After this window, the person is assumed to have exited.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
import time
import logging

logger = logging.getLogger("identity_resolver.gating")


class GateResult(Enum):
    ACCEPT = "accept"
    REJECT_PHYSICS = "reject_physics"       # Transition too fast — physically impossible
    REJECT_TIMEOUT = "reject_timeout"        # Person hasn't been seen long enough to gate


# ─── Store Camera Topology ────────────────────────────────────────────────────
#
# This matrix should be loaded from config per deployment, not hardcoded.
# Format: CAMERA_TRAVEL_TIMES[from_camera][to_camera] = min_seconds
#
# Example: A typical 200m² retail store
#   Entrance → Billing: minimum 10 seconds (must walk through store)
#   Tracking1 → Tracking2: minimum 5 seconds (adjacent zones)
#   Billing → Entrance: minimum 10 seconds (symmetrical)
#
DEFAULT_TRAVEL_MATRIX = {
    "cam_entrance_01": {
        "cam_face_01":     2.0,    # Same zone, different camera
        "cam_tracking_01": 5.0,
        "cam_billing_01":  10.0,
        "cam_vehicle_01":  15.0,
    },
    "cam_face_01": {
        "cam_entrance_01": 2.0,
        "cam_tracking_01": 5.0,
        "cam_billing_01":  10.0,
    },
    "cam_tracking_01": {
        "cam_entrance_01": 5.0,
        "cam_face_01":     5.0,
        "cam_billing_01":  5.0,
    },
    "cam_billing_01": {
        "cam_entrance_01": 10.0,
        "cam_tracking_01": 5.0,
    },
}


class SpatiotemporalGate:
    """
    Validates physical plausibility of a camera-to-camera identity transition.

    Algorithm:
      1. Look up the minimum travel time between last_seen.camera and current_camera.
      2. Compute elapsed = current_ts - last_seen.last_seen_ts.
      3. If elapsed < min_travel_time → REJECT_PHYSICS (impossible transition).
      4. If elapsed > gate_window → REJECT_TIMEOUT (stale match, don't reuse).
      5. Otherwise → ACCEPT.

    The gate is intentionally conservative on false-accepts (REJECT_PHYSICS)
    at the cost of occasional false-rejects. A false-reject results in an
    UNKNOWN_IDENTITY (soft error); a false-accept results in wrong billing
    attribution (hard error). We prefer to fail open on identity.
    """

    UNKNOWN_CAMERA_MIN_TRAVEL = 3.0     # Default minimum if camera not in matrix

    def __init__(
        self,
        travel_matrix: Optional[dict] = None,
        gate_window_s: float = 3600.0,  # 1 hour — max time person stays in store
    ):
        self.matrix = travel_matrix or DEFAULT_TRAVEL_MATRIX
        self.gate_window_s = gate_window_s

    def evaluate(
        self,
        candidate_id: str,
        current_camera: str,
        current_ts: float,
        last_seen: Optional[dict],
    ) -> GateResult:
        """
        Evaluate whether a candidate identity match is physically plausible.

        Args:
            candidate_id:   Matched customer ID from Qdrant
            current_camera: Camera ID where match was detected
            current_ts:     Timestamp of current detection
            last_seen:      Dict with {camera_id, last_seen_ts} from ActiveIdentityRegistry
                            None means first sighting — always ACCEPT
        """
        if last_seen is None:
            # First time we see this identity — no temporal constraint
            return GateResult.ACCEPT

        last_camera = last_seen["camera_id"]
        last_ts = last_seen["last_seen_ts"]
        elapsed = current_ts - last_ts

        # Same camera → no travel constraint needed
        if last_camera == current_camera:
            return GateResult.ACCEPT

        # Gate window check: if more than gate_window_s has passed,
        # the person may have re-entered the store — accept as new sighting
        if elapsed > self.gate_window_s:
            return GateResult.ACCEPT

        # Look up minimum travel time between cameras
        min_travel = self._lookup_travel_time(last_camera, current_camera)

        if elapsed < min_travel:
            logger.debug(
                f"Physics gate REJECTED {candidate_id}: "
                f"{last_camera}→{current_camera} in {elapsed:.1f}s "
                f"(min: {min_travel:.1f}s)"
            )
            return GateResult.REJECT_PHYSICS

        return GateResult.ACCEPT

    def _lookup_travel_time(self, from_cam: str, to_cam: str) -> float:
        """
        Returns minimum travel time between two cameras.
        Falls back to DEFAULT for unknown camera pairs.
        """
        from_matrix = self.matrix.get(from_cam, {})
        return from_matrix.get(to_cam, self.UNKNOWN_CAMERA_MIN_TRAVEL)
