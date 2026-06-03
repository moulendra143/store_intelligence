"""
detect.py — Complete detection pipeline.

Features:
- YOLOv8n + ByteTrack for person detection and multi-object tracking
- Entry/exit line crossing with direction detection (ENTRY / EXIT)
- Re-entry detection (REENTRY event for same visitor returning within window)
- Staff detection heuristic (aspect ratio + movement pattern)
- Zone tracking with polygon regions (ZONE_ENTER / ZONE_EXIT / ZONE_DWELL)
- Billing queue depth tracking (BILLING_QUEUE_JOIN / BILLING_QUEUE_ABANDON)
- Group entry handling (N persons crossing simultaneously → N separate ENTRY events)
- Cross-camera deduplication
- ISO-8601 UTC timestamps derived from clip filename date + frame offset
- Low-confidence events emitted with actual confidence (not suppressed)

Usage:
  Called by run.py — not executed directly.
"""

import cv2
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from ultralytics import YOLO

from emit import create_event, save_event, post_event
from session_manager import SessionManager
from tracker import ReIDEngine


# ------------------------------------------------------------------
# ZONE UTILITIES
# ------------------------------------------------------------------

def load_store_layout(store_id: str, layout_path: str = "data/store_layout.json") -> dict:
    """Load zone polygons for a store from store_layout.json."""
    if not os.path.exists(layout_path):
        return {}
    with open(layout_path) as f:
        layout = json.load(f)
    # Support both a list of stores or a single store dict
    if isinstance(layout, list):
        for store in layout:
            if store.get("store_id") == store_id:
                return store
        return {}
    return layout


def point_in_polygon(px: int, py: int, polygon: list[list[int]]) -> bool:
    """Ray casting algorithm for point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def get_zone_for_point(px: int, py: int, zones: list[dict]) -> Optional[str]:
    """Return the first zone whose polygon contains (px, py), or None."""
    for zone in zones:
        poly = zone.get("polygon", [])
        if poly and point_in_polygon(px, py, poly):
            return zone.get("zone_id") or zone.get("name")
    return None


def is_billing_zone(zone_id: Optional[str]) -> bool:
    """Check if a zone_id refers to the billing/checkout area."""
    if zone_id is None:
        return False
    return any(keyword in zone_id.upper() for keyword in ("BILLING", "CHECKOUT", "CASHIER", "POS", "COUNTER"))


# ------------------------------------------------------------------
# STAFF DETECTION HEURISTIC
# ------------------------------------------------------------------

def classify_staff(
    track_id: int,
    trajectory: list[tuple[int, int]],
    frame_width: int,
    frame_height: int,
    aspect_ratio: float,
) -> bool:
    """
    Lightweight staff detection heuristic.

    Assumptions (retail CCTV context):
    1. Staff tend to have wider/uniform bounding boxes (e.g., aprons make them appear wider).
       Aspect ratio < 0.5 is a mild signal.
    2. Staff traverse the entry line multiple times in short intervals (visible in trajectory).
    3. Staff stay near the perimeter / entry area more than shoppers.

    Returns True if the person is likely staff.
    """
    # Heuristic 1: Very unusual aspect ratio (very wide bbox) suggests uniform/apron
    if aspect_ratio < 0.35:
        return True

    # Heuristic 2: Frequent directional reversals near the entry line
    # (staff often cross the threshold area repeatedly without shoppers)
    if len(trajectory) >= 10:
        reversals = 0
        for i in range(2, len(trajectory)):
            dy_prev = trajectory[i - 1][1] - trajectory[i - 2][1]
            dy_curr = trajectory[i][1] - trajectory[i - 1][1]
            if dy_prev != 0 and dy_curr != 0 and (dy_prev > 0) != (dy_curr > 0):
                reversals += 1
        if reversals >= 4:
            return True

    return False


# ------------------------------------------------------------------
# TIMESTAMP UTILITIES
# ------------------------------------------------------------------

def parse_clip_start_time(video_path: str) -> datetime:
    """
    Attempt to extract a start timestamp from the video filename.
    Expected patterns:  20260303_142200.mp4  or  CAM_ENTRY_01_2026-03-03T14:22:00.mp4
    Falls back to a fixed demo date if no date found.
    """
    stem = Path(video_path).stem
    # Try YYYYMMDD_HHMMSS
    m = re.search(r"(\d{8})_(\d{6})", stem)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y%m%d %H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Try ISO-like
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", stem)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Default demo start time
    return datetime(2026, 3, 3, 9, 0, 0, tzinfo=timezone.utc)


def frame_to_timestamp(clip_start: datetime, frame_number: int, fps: float) -> str:
    """Convert frame index to ISO-8601 UTC timestamp."""
    offset_seconds = frame_number / max(fps, 1.0)
    ts = clip_start + timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------
# MAIN PIPELINE CLASS
# ------------------------------------------------------------------

class DetectionPipeline:
    """
    Full detection pipeline for a single video clip.
    """

    def __init__(
        self,
        video_path: str,
        store_id: str,
        camera_id: str,
        model_path: str = "yolov8n.pt",
        entry_line_y: Optional[int] = None,
        layout_path: str = "data/store_layout.json",
        min_confidence: float = 0.35,
        events_output: str = "data/events.jsonl",
        api_url: Optional[str] = None,
        session_manager: Optional[SessionManager] = None,
        reid_engine: Optional[ReIDEngine] = None,
        headless: bool = True,
    ):
        self.video_path = video_path
        self.store_id = store_id
        self.camera_id = camera_id
        self.min_confidence = min_confidence
        self.events_output = events_output
        self.api_url = api_url
        self.headless = headless

        # Model
        self.model = YOLO(model_path)

        # Shared state (can be shared across cameras of the same store)
        self.session_manager = session_manager or SessionManager()
        self.reid_engine = reid_engine or ReIDEngine()

        # Store layout (zones)
        self.store_layout = load_store_layout(store_id, layout_path)
        self.zones = self.store_layout.get("zones", [])

        # Entry/exit line
        self.entry_line_y = entry_line_y  # Will be auto-detected if None

        # State
        self.clip_start = parse_clip_start_time(video_path)
        self.frame_number = 0
        self.fps = 15.0  # Will be updated from video metadata

        # Per-track trajectory history for staff detection
        self._trajectories: dict[int, list[tuple[int, int]]] = {}

        # Tracks currently inside store (crossed entry line inbound)
        self._inside_store: set[str] = set()

        # Event counters for summary
        self.stats = {
            "entries": 0,
            "exits": 0,
            "reentries": 0,
            "zone_enters": 0,
            "zone_dwells": 0,
            "billing_joins": 0,
            "billing_abandons": 0,
            "staff_movements": 0,
            "total_events": 0,
        }

    def _emit(self, event: dict):
        """Save event to file and optionally POST to API."""
        save_event(event, self.events_output)
        if self.api_url:
            post_event(event, self.api_url)
        self.stats["total_events"] += 1
        
        # Log to console for visibility
        staff_icon = "👔" if event.get("is_staff") else "👤"
        zone_info = f" ({event.get('zone_id')})" if event.get('zone_id') else ""
        print(f"[{self.camera_id}] Frame {self.frame_number:4d} — {staff_icon} {event['event_type']}{zone_info} | Visitor: {event['visitor_id']}")

    def _get_timestamp(self) -> str:
        return frame_to_timestamp(self.clip_start, self.frame_number, self.fps)

    def _auto_detect_entry_line(self, frame_height: int) -> int:
        """
        Fallback: place the entry line at 60% height for entry cameras,
        or use a sensible default if we can't determine camera type.
        """
        if "ENTRY" in self.camera_id.upper():
            return int(frame_height * 0.60)
        if "BILLING" in self.camera_id.upper():
            return int(frame_height * 0.50)
        return int(frame_height * 0.55)

    def _process_entry_exit(
        self,
        visitor_id: str,
        track_id: int,
        center_y: int,
        confidence: float,
        is_staff: bool,
    ):
        """Handle ENTRY / EXIT / REENTRY crossing events."""
        session = self.session_manager

        # Previous position for direction detection
        prev_y = self._trajectories.get(track_id, [])
        prev_y_val = prev_y[-1][1] if prev_y else None

        if prev_y_val is None:
            return

        # Minimum travel required to trigger crossing (avoids flicker)
        CROSSING_BUFFER = 8

        # --- ENTRY ---
        if (
            prev_y_val < self.entry_line_y - CROSSING_BUFFER
            and center_y >= self.entry_line_y
            and visitor_id not in self._inside_store
        ):
            self._inside_store.add(visitor_id)

            if not session.has_session(visitor_id):
                # First ever entry
                session.create_session(visitor_id, is_staff=is_staff)
                seq = session.next_seq(visitor_id)
                event = create_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=visitor_id,
                    event_type="ENTRY",
                    is_staff=is_staff,
                    confidence=confidence,
                    timestamp=self._get_timestamp(),
                    metadata={"session_seq": seq},
                )
                self._emit(event)
                self.stats["entries"] += 1
                if is_staff:
                    self.stats["staff_movements"] += 1

            elif session.is_reentry(visitor_id):
                # Re-entry
                session.mark_reentry(visitor_id, is_staff=is_staff)
                seq = session.next_seq(visitor_id)
                event = create_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=visitor_id,
                    event_type="REENTRY",
                    is_staff=is_staff,
                    confidence=confidence,
                    timestamp=self._get_timestamp(),
                    metadata={"session_seq": seq},
                )
                self._emit(event)
                self.stats["reentries"] += 1

        # --- EXIT ---
        elif (
            prev_y_val > self.entry_line_y + CROSSING_BUFFER
            and center_y <= self.entry_line_y
            and visitor_id in self._inside_store
        ):
            self._inside_store.discard(visitor_id)

            # Check billing abandon before exit
            if session.is_inside(visitor_id) and session.get_session(visitor_id).get("in_billing_zone"):
                seq = session.next_seq(visitor_id)
                abandon_event = create_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=visitor_id,
                    event_type="BILLING_QUEUE_ABANDON",
                    zone_id="BILLING",
                    is_staff=is_staff,
                    confidence=confidence,
                    timestamp=self._get_timestamp(),
                    metadata={"session_seq": seq, "queue_depth": self.session_manager.billing_queue_depth},
                )
                self._emit(abandon_event)
                self.stats["billing_abandons"] += 1

            session.exit_session(visitor_id)
            self.reid_engine.record_exit(self.camera_id, track_id)

            seq = session.next_seq(visitor_id)
            event = create_event(
                store_id=self.store_id,
                camera_id=self.camera_id,
                visitor_id=visitor_id,
                event_type="EXIT",
                is_staff=is_staff,
                confidence=confidence,
                timestamp=self._get_timestamp(),
                metadata={"session_seq": seq},
            )
            self._emit(event)
            self.stats["exits"] += 1

    def _process_zones(self, visitor_id: str, center_x: int, center_y: int, confidence: float, is_staff: bool):
        """Handle ZONE_ENTER / ZONE_EXIT / ZONE_DWELL / BILLING events."""
        if not self.zones:
            return
        if not self.session_manager.has_session(visitor_id):
            return
        if not self.session_manager.is_inside(visitor_id):
            return

        session = self.session_manager
        current_zone_in_session = session.get_session(visitor_id).get("current_zone")
        detected_zone = get_zone_for_point(center_x, center_y, self.zones)

        if detected_zone != current_zone_in_session:
            # Zone changed
            if current_zone_in_session is not None:
                # ZONE_EXIT from previous zone
                dwell_ms = session.get_zone_dwell_ms(visitor_id)

                # Billing abandon: left billing zone without a transaction
                if is_billing_zone(current_zone_in_session):
                    was_in_billing = session.leave_billing_zone(visitor_id)
                    if was_in_billing and not is_staff:
                        seq = session.next_seq(visitor_id)
                        abandon_event = create_event(
                            store_id=self.store_id,
                            camera_id=self.camera_id,
                            visitor_id=visitor_id,
                            event_type="BILLING_QUEUE_ABANDON",
                            zone_id=current_zone_in_session,
                            is_staff=is_staff,
                            confidence=confidence,
                            timestamp=self._get_timestamp(),
                            metadata={
                                "session_seq": seq,
                                "queue_depth": self.session_manager.billing_queue_depth,
                            },
                        )
                        self._emit(abandon_event)
                        self.stats["billing_abandons"] += 1

                session.exit_zone(visitor_id)
                seq = session.next_seq(visitor_id)
                zone_exit_event = create_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=visitor_id,
                    event_type="ZONE_EXIT",
                    zone_id=current_zone_in_session,
                    dwell_ms=dwell_ms,
                    is_staff=is_staff,
                    confidence=confidence,
                    timestamp=self._get_timestamp(),
                    metadata={"session_seq": seq},
                )
                self._emit(zone_exit_event)

            if detected_zone is not None:
                # ZONE_ENTER into new zone
                session.enter_zone(visitor_id, detected_zone)
                seq = session.next_seq(visitor_id)

                meta = {"session_seq": seq, "sku_zone": detected_zone}

                # Billing queue join
                if is_billing_zone(detected_zone) and not is_staff:
                    prev_depth = self.session_manager.billing_queue_depth
                    session.enter_billing_zone(visitor_id)
                    if prev_depth > 0:
                        meta["queue_depth"] = self.session_manager.billing_queue_depth
                        zone_enter_event_type = "BILLING_QUEUE_JOIN"
                        self.stats["billing_joins"] += 1
                    else:
                        zone_enter_event_type = "ZONE_ENTER"
                else:
                    zone_enter_event_type = "ZONE_ENTER"

                zone_enter_event = create_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=visitor_id,
                    event_type=zone_enter_event_type,
                    zone_id=detected_zone,
                    is_staff=is_staff,
                    confidence=confidence,
                    timestamp=self._get_timestamp(),
                    metadata=meta,
                )
                self._emit(zone_enter_event)
                self.stats["zone_enters"] += 1

        # Check if ZONE_DWELL should be emitted
        dwell_ms = session.should_emit_dwell(visitor_id)
        if dwell_ms is not None and detected_zone is not None:
            seq = session.next_seq(visitor_id)
            dwell_event = create_event(
                store_id=self.store_id,
                camera_id=self.camera_id,
                visitor_id=visitor_id,
                event_type="ZONE_DWELL",
                zone_id=detected_zone,
                dwell_ms=dwell_ms,
                is_staff=is_staff,
                confidence=confidence,
                timestamp=self._get_timestamp(),
                metadata={"session_seq": seq, "sku_zone": detected_zone},
            )
            self._emit(dwell_event)
            self.stats["zone_dwells"] += 1

    def run(self) -> dict:
        """
        Process the video clip. Returns stats dict on completion.
        """
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {self.video_path}")

        self.fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Auto-detect entry line if not provided
        if self.entry_line_y is None:
            self.entry_line_y = self._auto_detect_entry_line(frame_height)

        print(f"\n{'='*60}")
        print(f"Processing: {self.video_path}")
        print(f"  Store: {self.store_id} | Camera: {self.camera_id}")
        print(f"  FPS: {self.fps} | Resolution: {frame_width}x{frame_height}")
        print(f"  Entry line Y: {self.entry_line_y}")
        print(f"  Zones: {[z.get('zone_id', z.get('name')) for z in self.zones]}")
        print(f"{'='*60}\n")

        # Group entry detection: tracks that cross the line in the same frame window
        group_window_frames = 5
        crossing_buffer: list[dict] = []

        while True:
            success, frame = cap.read()
            if not success:
                print(f"\n✓ Video processing complete: {Path(self.video_path).name}")
                break

            self.frame_number += 1

            # --- YOLOv8 + ByteTrack ---
            results = self.model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                classes=[0],  # person class only
                verbose=False,
                conf=self.min_confidence,
            )

            active_track_ids: set[int] = set()

            if results[0].boxes is not None:
                boxes_this_frame = results[0].boxes

                for box in boxes_this_frame:
                    if box.id is None:
                        continue

                    track_id = int(box.id.item())
                    confidence = float(box.conf.item())
                    active_track_ids.add(track_id)

                    x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                    center_x = (x1 + x2) // 2
                    center_y = (y1 + y2) // 2
                    w = x2 - x1
                    h = y2 - y1
                    aspect = w / max(h, 1)

                    # Update trajectory (cap at 30 frames to keep memory bounded)
                    if track_id not in self._trajectories:
                        self._trajectories[track_id] = []
                    self._trajectories[track_id].append((center_x, center_y))
                    if len(self._trajectories[track_id]) > 30:
                        self._trajectories[track_id].pop(0)

                    # Staff classification
                    is_staff = classify_staff(
                        track_id,
                        self._trajectories[track_id],
                        frame_width,
                        frame_height,
                        aspect,
                    )

                    # Re-ID: get or create visitor_id
                    visitor_id, is_new = self.reid_engine.get_or_create_visitor(
                        self.camera_id, track_id, x1, y1, x2, y2
                    )

                    # Cross-camera deduplication
                    now_dt = self.clip_start + timedelta(seconds=self.frame_number / self.fps)
                    if self.reid_engine.is_duplicate_cross_camera(visitor_id, self.camera_id, now_dt):
                        continue  # This camera is double-counting — skip

                    # Update session heartbeat
                    if self.session_manager.has_session(visitor_id):
                        self.session_manager.update_seen(visitor_id)

                    # Entry/exit line crossing
                    self._process_entry_exit(visitor_id, track_id, center_y, confidence, is_staff)

                    # Zone tracking
                    self._process_zones(visitor_id, center_x, center_y, confidence, is_staff)

                # Visualisation (only in non-headless mode)
                if not self.headless:
                    annotated = results[0].plot()
                    cv2.line(annotated, (0, self.entry_line_y), (frame_width, self.entry_line_y), (0, 255, 0), 2)
                    cv2.putText(annotated, "ENTRY/EXIT", (10, self.entry_line_y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.imshow("Store Intelligence", annotated)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

            # Timeout stale sessions every 100 frames
            if self.frame_number % 100 == 0:
                stale = self.session_manager.timeout_stale_sessions()
                for sid in stale:
                    print(f"  [TIMEOUT] Session timed out: {sid}")

            # Clean up stale tracks from Re-ID engine
            self.reid_engine.cleanup_stale_tracks(self.camera_id, active_track_ids)

        cap.release()
        if not self.headless:
            cv2.destroyAllWindows()

        # Print summary
        print(f"\n{'─'*60}")
        print(f"PIPELINE SUMMARY — {self.camera_id}")
        for k, v in self.stats.items():
            print(f"  {k:25s}: {v}")
        print(f"{'─'*60}\n")

        return self.stats