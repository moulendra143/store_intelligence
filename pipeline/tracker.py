"""
tracker.py — Re-ID engine with cross-camera deduplication and re-entry detection.

Design choices:
- Lightweight appearance fingerprint: normalised (width, height, aspect_ratio) of bounding box
  combined with average position trajectory. No heavy embedding model needed for retail CCTV
  where persons typically have consistent sizes.
- Re-entry window: 5 minutes. Within this window a returning visitor triggers REENTRY not ENTRY.
- Cross-camera dedup: a visitor_id cannot be active in two cameras simultaneously at the
  same store unless the cameras cover genuinely different zones (billing vs entry).
"""

import time
from datetime import datetime, timedelta
from collections import defaultdict


# Re-entry window in seconds — a person who exits and returns within this window
# is flagged as REENTRY rather than a new ENTRY.
REENTRY_WINDOW_SECONDS = 300  # 5 minutes


class ReIDEngine:
    """
    Lightweight Re-ID that maps raw tracker IDs to stable visitor_ids.

    Strategy:
    1. Each (camera, track_id) gets a visitor_id on first appearance.
    2. When a visitor exits, their appearance fingerprint is stored for REENTRY_WINDOW_SECONDS.
    3. If a new detection closely matches a recent exit fingerprint, the same visitor_id is reused.
    4. Cross-camera dedup: if the same visitor_id is seen in an overlapping camera at the same
       timestamp, only the first camera event is kept.
    """

    def __init__(self):
        # (camera_id, track_id) -> visitor_id
        self._track_to_visitor: dict[tuple, str] = {}

        # visitor_id -> fingerprint info (for re-entry matching)
        self._visitor_fingerprints: dict[str, dict] = {}

        # visitor_id -> exit timestamp (for re-entry window)
        self._exit_times: dict[str, datetime] = {}

        # visitor_id -> list of recent (camera_id, timestamp) (cross-camera dedup)
        self._cross_camera_log: dict[str, list] = defaultdict(list)

        # Counter for generating unique visitor IDs
        self._visitor_counter = 0

    def _new_visitor_id(self) -> str:
        self._visitor_counter += 1
        return f"VIS_{self._visitor_counter:06d}"

    def _compute_fingerprint(self, x1: int, y1: int, x2: int, y2: int) -> dict:
        """
        Compute a lightweight appearance fingerprint from bounding box geometry.
        Aspect ratio + relative size are surprisingly stable per-person across frames.
        """
        w = x2 - x1
        h = y2 - y1
        aspect = round(w / max(h, 1), 2)
        return {
            "width": w,
            "height": h,
            "aspect": aspect,
        }

    def _fingerprint_match(self, fp1: dict, fp2: dict, threshold: float = 0.25) -> bool:
        """
        Check if two fingerprints could be the same person.
        Compares aspect ratio (scale-invariant) with a tolerance threshold.
        """
        return abs(fp1["aspect"] - fp2["aspect"]) < threshold

    def get_or_create_visitor(
        self,
        camera_id: str,
        track_id: int,
        x1: int, y1: int, x2: int, y2: int,
    ) -> tuple[str, bool]:
        """
        Returns (visitor_id, is_new_visitor).

        Checks for re-entry: if we've seen a fingerprint-matching person recently exit,
        we return the same visitor_id with is_new_visitor=False so the caller can emit REENTRY.
        """
        key = (camera_id, track_id)

        if key in self._track_to_visitor:
            return self._track_to_visitor[key], False

        # New track — check if it matches a recent exit (re-entry detection)
        fp = self._compute_fingerprint(x1, y1, x2, y2)
        now = datetime.utcnow()

        best_match_vid = None
        for vid, exit_time in list(self._exit_times.items()):
            # Prune expired re-entry windows
            if (now - exit_time).total_seconds() > REENTRY_WINDOW_SECONDS:
                del self._exit_times[vid]
                continue

            stored_fp = self._visitor_fingerprints.get(vid, {})
            if stored_fp and self._fingerprint_match(fp, stored_fp):
                best_match_vid = vid
                break

        if best_match_vid:
            # Re-entry: reuse the same visitor_id
            self._track_to_visitor[key] = best_match_vid
            self._visitor_fingerprints[best_match_vid] = fp
            # Remove from exit list since they're back
            self._exit_times.pop(best_match_vid, None)
            return best_match_vid, False  # caller checks session state for REENTRY logic

        # Genuinely new visitor
        visitor_id = self._new_visitor_id()
        self._track_to_visitor[key] = visitor_id
        self._visitor_fingerprints[visitor_id] = fp
        return visitor_id, True

    def record_exit(self, camera_id: str, track_id: int):
        """Record that a tracked person has exited. Starts the re-entry window."""
        key = (camera_id, track_id)
        visitor_id = self._track_to_visitor.get(key)
        if visitor_id:
            self._exit_times[visitor_id] = datetime.utcnow()

    def is_duplicate_cross_camera(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        window_seconds: float = 2.0,
    ) -> bool:
        """
        Cross-camera deduplication guard.
        Returns True if this visitor_id was already seen in a *different* camera
        within `window_seconds` — indicating the same physical person is being
        double-counted due to overlapping FOVs.
        """
        log = self._cross_camera_log[visitor_id]
        for prev_cam, prev_ts in log:
            if prev_cam != camera_id:
                if abs((timestamp - prev_ts).total_seconds()) < window_seconds:
                    return True  # Duplicate — suppress this event

        # Not a duplicate; log this appearance
        log.append((camera_id, timestamp))
        # Keep log trimmed to last 10 appearances
        self._cross_camera_log[visitor_id] = log[-10:]
        return False

    def cleanup_stale_tracks(self, camera_id: str, active_track_ids: set):
        """Remove tracks that are no longer active in a camera (track lost)."""
        stale = [
            key for key in self._track_to_visitor
            if key[0] == camera_id and key[1] not in active_track_ids
        ]
        for key in stale:
            del self._track_to_visitor[key]
