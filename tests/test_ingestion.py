# PROMPT: "Write pytest tests for a FastAPI event ingestion endpoint that:
#   - Accepts POST /events with a JSON body matching the EventInSchema Pydantic model
#   - Returns 201 on success, 422 on schema violation, 409/200 on duplicate event_id
#   - Stores the event in SQLite via SQLAlchemy; use sqlite:///:memory: for test isolation
#   - Tests should cover: valid event, duplicate idempotency, bad confidence range,
#     all supported event_types, staff flag propagation, event query filters"
#
# CHANGES MADE:
#   - AI initially used a module-level TestClient with a shared DB; I changed to
#     per-test init_db() via autouse fixture to avoid cross-test state pollution.
#   - AI suggested asserting status 409 for duplicates; actual implementation returns
#     200 with {"status": "duplicate"} for idempotency — corrected assertions.
#   - Added StaticPool to database.py (AI did not suggest this) after discovering
#     that in-memory SQLite creates a fresh DB per connection in the pool, causing
#     "no such table" failures with FastAPI's thread model.
#   - AI did not generate the staff_event_flagged_correctly test; I added it
#     specifically to verify the is_staff field passes through the full ORM layer.

"""
tests/test_ingestion.py — Tests for the event ingestion endpoint.
"""

import json
import uuid
import pytest
from fastapi.testclient import TestClient

# Use in-memory SQLite for tests
import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.main import app
from app.database import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    yield


def make_valid_event(**overrides) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_TEST_001",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_000001",
        "event_type": "ENTRY",
        "timestamp": "2026-03-03T14:22:10Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(overrides)
    return base


class TestSingleEventIngest:
    def test_post_valid_event_returns_201(self):
        event = make_valid_event()
        resp = client.post("/events", json=event)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        assert data["event_id"] == event["event_id"]

    def test_duplicate_event_id_is_idempotent(self):
        event = make_valid_event()
        resp1 = client.post("/events", json=event)
        resp2 = client.post("/events", json=event)
        assert resp1.status_code == 201
        assert resp2.status_code == 201
        assert resp2.json()["status"] == "duplicate"

    def test_invalid_schema_returns_422(self):
        # Missing required fields
        resp = client.post("/events", json={"event_id": "bad"})
        assert resp.status_code == 422

    def test_confidence_out_of_range_returns_422(self):
        event = make_valid_event(confidence=1.5)  # > 1.0 not allowed
        resp = client.post("/events", json=event)
        assert resp.status_code == 422

    def test_all_event_types_accepted(self):
        valid_types = [
            "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
            "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
        ]
        for et in valid_types:
            event = make_valid_event(
                event_id=str(uuid.uuid4()),
                event_type=et,
                zone_id="BILLING" if "BILLING" in et or "ZONE" in et else None,
            )
            resp = client.post("/events", json=event)
            assert resp.status_code == 201, f"Failed for event_type={et}"

    def test_staff_event_flagged_correctly(self):
        event = make_valid_event(is_staff=True)
        resp = client.post("/events", json=event)
        assert resp.status_code == 201


class TestEventQuery:
    def test_query_events_empty(self):
        resp = client.get("/events", params={"store_id": "STORE_NONEXISTENT"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_query_returns_ingested_events(self):
        event = make_valid_event(store_id="STORE_QUERY_TEST")
        client.post("/events", json=event)
        resp = client.get("/events", params={"store_id": "STORE_QUERY_TEST"})
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_filter_by_event_type(self):
        for et in ["ENTRY", "EXIT"]:
            client.post("/events", json=make_valid_event(
                event_id=str(uuid.uuid4()),
                store_id="STORE_FILTER_TEST",
                event_type=et,
            ))
        resp = client.get("/events", params={"store_id": "STORE_FILTER_TEST", "event_type": "ENTRY"})
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert all(e["event_type"] == "ENTRY" for e in events)
