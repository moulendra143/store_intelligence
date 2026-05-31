"""
session_manager.py — Per-visitor session state with zone dwell tracking,
staff classification, billing queue depth, and session sequence counters.
"""

from datetime import datetime
from typing import Optional


# Emit a ZONE_DWELL event every N seconds of continuous zone presence
ZONE_DWELL_INTERVAL_SECONDS = 30

# Time without detection before a session is considered stale (visitor left without crossing exit line)
SESSION_TIMEOUT_SECONDS = 120


class SessionManager:
    """
    Manages lifecycle of visitor sessions including:
    - Entry / Exit timestamps
    - Zone enter/exit and periodic dwell events
    - Re-entry detection state
    - Staff classification
    - Session event sequence counter
    - Billing zone queue depth (global, shared across all tracked persons)
    """

    def __init__(self):
        # visitor_id -> session dict
        self.sessions: dict[str, dict] = {}

        # visitor_id -> last exit time (for re-entry detection by session_manager)
        self.last_exits: dict[str, datetime] = {}

        # Global billing queue depth (number of people currently in billing zone)
        self.billing_queue_depth: int = 0

    # ------------------------------------------------------------------
    # SESSION LIFECYCLE
    # ------------------------------------------------------------------

    def create_session(self, visitor_id: str, is_staff: bool = False):
        """Create a brand-new session for a visitor."""
        now = datetime.utcnow()
        self.sessions[visitor_id] = {
            "visitor_id": visitor_id,
            "entry_time": now,
            "last_seen": now,
            "exit_time": None,
            "inside": True,
            "is_staff": is_staff,
            # Zone tracking
            "current_zone": None,
            "zone_enter_time": None,
            "last_dwell_emit_time": None,
            "visited_zones": set(),
            # Billing
            "in_billing_zone": False,
            "billing_entry_time": None,
            # Session sequence
            "event_seq": 0,
            # Re-entry
            "reentry_count": 0,
        }

    def update_seen(self, visitor_id: str):
        """Heartbeat — update last seen time to prevent timeout."""
        if visitor_id in self.sessions:
            self.sessions[visitor_id]["last_seen"] = datetime.utcnow()

    def exit_session(self, visitor_id: str):
        """Mark visitor as exited."""
        if visitor_id in self.sessions:
            now = datetime.utcnow()
            self.sessions[visitor_id]["inside"] = False
            self.sessions[visitor_id]["exit_time"] = now
            self.last_exits[visitor_id] = now

            # Leave billing zone if they were in it
            if self.sessions[visitor_id]["in_billing_zone"]:
                self._leave_billing_zone(visitor_id)

            # Leave current zone
            self.sessions[visitor_id]["current_zone"] = None
            self.sessions[visitor_id]["zone_enter_time"] = None

    def mark_reentry(self, visitor_id: str, is_staff: bool = False):
        """Re-entry: visitor returns after exit."""
        if visitor_id in self.sessions:
            now = datetime.utcnow()
            self.sessions[visitor_id]["inside"] = True
            self.sessions[visitor_id]["reentry_count"] += 1
            self.sessions[visitor_id]["last_seen"] = now
            if is_staff:
                self.sessions[visitor_id]["is_staff"] = True

    # ------------------------------------------------------------------
    # ZONE TRACKING
    # ------------------------------------------------------------------

    def enter_zone(self, visitor_id: str, zone_id: str) -> bool:
        """
        Record zone entry. Returns True if this is a new zone (to emit ZONE_ENTER).
        """
        if visitor_id not in self.sessions:
            return False

        session = self.sessions[visitor_id]
        now = datetime.utcnow()

        if session["current_zone"] == zone_id:
            return False  # Already in this zone

        session["current_zone"] = zone_id
        session["zone_enter_time"] = now
        session["last_dwell_emit_time"] = now
        session["visited_zones"].add(zone_id)
        return True

    def exit_zone(self, visitor_id: str) -> Optional[str]:
        """
        Record zone exit. Returns the zone_id just left, or None.
        """
        if visitor_id not in self.sessions:
            return None
        session = self.sessions[visitor_id]
        left_zone = session["current_zone"]
        session["current_zone"] = None
        session["zone_enter_time"] = None
        session["last_dwell_emit_time"] = None
        return left_zone

    def should_emit_dwell(self, visitor_id: str) -> Optional[int]:
        """
        Check if a ZONE_DWELL event should be emitted.
        Returns dwell_ms since last dwell emit if >= ZONE_DWELL_INTERVAL_SECONDS, else None.
        """
        if visitor_id not in self.sessions:
            return None
        session = self.sessions[visitor_id]
        if session["current_zone"] is None:
            return None
        if session["last_dwell_emit_time"] is None:
            return None

        now = datetime.utcnow()
        elapsed = (now - session["last_dwell_emit_time"]).total_seconds()
        if elapsed >= ZONE_DWELL_INTERVAL_SECONDS:
            session["last_dwell_emit_time"] = now
            return int(elapsed * 1000)
        return None

    def get_zone_dwell_ms(self, visitor_id: str) -> int:
        """Total ms spent in current zone so far."""
        if visitor_id not in self.sessions:
            return 0
        session = self.sessions[visitor_id]
        if session["zone_enter_time"] is None:
            return 0
        return int((datetime.utcnow() - session["zone_enter_time"]).total_seconds() * 1000)

    # ------------------------------------------------------------------
    # BILLING ZONE
    # ------------------------------------------------------------------

    def enter_billing_zone(self, visitor_id: str):
        """Mark visitor as entering billing zone. Increments queue depth."""
        if visitor_id not in self.sessions:
            return
        session = self.sessions[visitor_id]
        if not session["in_billing_zone"]:
            session["in_billing_zone"] = True
            session["billing_entry_time"] = datetime.utcnow()
            if not session["is_staff"]:
                self.billing_queue_depth += 1

    def _leave_billing_zone(self, visitor_id: str):
        """Internal: decrement billing queue depth."""
        if visitor_id not in self.sessions:
            return
        session = self.sessions[visitor_id]
        if session["in_billing_zone"]:
            session["in_billing_zone"] = False
            if not session["is_staff"]:
                self.billing_queue_depth = max(0, self.billing_queue_depth - 1)

    def leave_billing_zone(self, visitor_id: str) -> bool:
        """
        Mark visitor as leaving billing zone.
        Returns True if they were in the billing zone (caller decides if ABANDON event needed).
        """
        if visitor_id not in self.sessions:
            return False
        was_in_billing = self.sessions[visitor_id]["in_billing_zone"]
        self._leave_billing_zone(visitor_id)
        return was_in_billing

    # ------------------------------------------------------------------
    # SEQUENCE COUNTER
    # ------------------------------------------------------------------

    def next_seq(self, visitor_id: str) -> int:
        """Return next event sequence number for this visitor's session."""
        if visitor_id not in self.sessions:
            return 0
        self.sessions[visitor_id]["event_seq"] += 1
        return self.sessions[visitor_id]["event_seq"]

    # ------------------------------------------------------------------
    # QUERY HELPERS
    # ------------------------------------------------------------------

    def has_session(self, visitor_id: str) -> bool:
        return visitor_id in self.sessions

    def is_inside(self, visitor_id: str) -> bool:
        return self.sessions.get(visitor_id, {}).get("inside", False)

    def is_reentry(self, visitor_id: str) -> bool:
        """True if visitor has previously exited (i.e., this would be a re-entry)."""
        return visitor_id in self.last_exits

    def get_session(self, visitor_id: str) -> Optional[dict]:
        return self.sessions.get(visitor_id)

    def is_staff(self, visitor_id: str) -> bool:
        return self.sessions.get(visitor_id, {}).get("is_staff", False)

    def get_all_active(self) -> list[str]:
        """Return visitor_ids currently inside the store."""
        return [vid for vid, s in self.sessions.items() if s["inside"]]

    def timeout_stale_sessions(self) -> list[str]:
        """
        Expire sessions for visitors who haven't been seen recently.
        Returns list of visitor_ids that were timed out.
        """
        now = datetime.utcnow()
        timed_out = []
        for visitor_id, session in self.sessions.items():
            if session["inside"]:
                elapsed = (now - session["last_seen"]).total_seconds()
                if elapsed > SESSION_TIMEOUT_SECONDS:
                    self.exit_session(visitor_id)
                    timed_out.append(visitor_id)
        return timed_out