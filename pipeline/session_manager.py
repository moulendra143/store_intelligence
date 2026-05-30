from datetime import datetime


class SessionManager:

    def __init__(self):

        self.sessions = {}

        self.last_exits = {}

    def create_session(self, visitor_id):

        self.sessions[visitor_id] = {
            "visitor_id": visitor_id,
            "entry_time": datetime.utcnow(),
            "last_seen": datetime.utcnow(),
            "exit_time": None,
            "inside": True,
            "is_staff": False,
            "zones": set(),
            "last_zone": None,
            "dwell_start": None,
            "reentry_count": 0
        }

    def update_seen(self, visitor_id):

        if visitor_id in self.sessions:
            self.sessions[visitor_id]["last_seen"] = datetime.utcnow()

    def exit_session(self, visitor_id):

        if visitor_id in self.sessions:

            now = datetime.utcnow()

            self.sessions[visitor_id]["inside"] = False
            self.sessions[visitor_id]["exit_time"] = now

            self.last_exits[visitor_id] = now

    def mark_reentry(self, visitor_id):

        if visitor_id in self.sessions:

            self.sessions[visitor_id]["inside"] = True

            self.sessions[visitor_id]["reentry_count"] += 1

            self.sessions[visitor_id]["last_seen"] = datetime.utcnow()

    def is_reentry(self, visitor_id):

        return visitor_id in self.last_exits

    def has_session(self, visitor_id):

        return visitor_id in self.sessions

    def get_session(self, visitor_id):

        return self.sessions.get(visitor_id)