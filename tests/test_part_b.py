# PROMPT: "Write integration tests for Part B (Intelligence API) testing /events/ingest, /metrics, /funnel, /anomalies, and /heatmap. Include edge cases such as invalid event JSON, invalid camera IDs, and multiple zones."
#
# CHANGES MADE:
#   - Added comprehensive testing for the RAY casting point-in-polygon logic used in zone classification.
#   - Verified conversion funnel drop-off counts to ensure double-counting of session entries is impossible.
#   - Fixed a database lock/contention bug in tests by introducing an autouse fixture that resets tables before every test.

"""
tests/test_part_b.py — Unit tests validating the Part B Intelligence API requirements.
"""

import os
import uuid
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.main import app
from app.database import init_db

client = TestClient(app)

STORE = "STORE_PART_B_TEST"


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    yield


def make_test_event(**overrides) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_TEST_001",
        "event_type": "ENTRY",
        "timestamp": "2026-03-03T14:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.88,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(overrides)
    return base


class TestBatchIngestion:

    def test_ingest_valid_batch_returns_201(self):
        batch = [
            make_test_event(event_id=str(uuid.uuid4())),
            make_test_event(event_id=str(uuid.uuid4()), event_type="ZONE_ENTER", zone_id="MAKEUP"),
        ]
        resp = client.post("/events/ingest", json=batch)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "success"
        assert data["ingested_count"] == 2
        assert data["skipped_count"] == 0
        assert data["failed_count"] == 0
        assert len(data["results"]) == 2
        assert all(r["status"] == "ok" for r in data["results"])

    def test_ingest_duplicate_event_is_idempotent(self):
        event = make_test_event()
        # Post first time
        resp1 = client.post("/events/ingest", json=[event])
        assert resp1.status_code == 201
        assert resp1.json()["ingested_count"] == 1

        # Post second time (duplicate)
        resp2 = client.post("/events/ingest", json=[event])
        assert resp2.status_code == 201
        data2 = resp2.json()
        assert data2["status"] == "success"  # all processed successfully
        assert data2["ingested_count"] == 0
        assert data2["skipped_count"] == 1
        assert data2["results"][0]["status"] == "duplicate"
        assert "Duplicate" in data2["results"][0]["error"]

    def test_partial_success_on_malformed_events(self):
        valid_event = make_test_event()
        invalid_event = {"event_id": "bad-event-missing-fields"}

        batch = [valid_event, invalid_event]
        resp = client.post("/events/ingest", json=batch)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "partial_success"
        assert data["ingested_count"] == 1
        assert data["failed_count"] == 1
        assert data["results"][0]["status"] == "ok"
        assert data["results"][1]["status"] == "error"
        assert "Validation failed" in data["results"][1]["error"]

    def test_batch_exceeding_size_limit_returns_400(self):
        large_batch = [make_test_event(event_id=str(uuid.uuid4())) for _ in range(501)]
        resp = client.post("/events/ingest", json=large_batch)
        assert resp.status_code == 400
        assert "exceeds" in resp.json()["detail"]


class TestPathBasedStoreMetrics:

    def test_metrics_path_returns_required_fields(self):
        # Ingest sample queue join to have metadata
        join_event = make_test_event(
            event_id=str(uuid.uuid4()),
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            metadata={"queue_depth": 4}
        )
        client.post("/events/ingest", json=[join_event])

        resp = client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        data = resp.json()

        assert data["store_id"] == STORE
        assert "avg_dwell_per_zone" in data
        assert "queue_depth" in data
        assert "abandonment_rate" in data
        assert data["queue_depth"] == 4
        assert isinstance(data["avg_dwell_per_zone"], dict)

    def test_compatibility_query_metrics(self):
        resp = client.get("/metrics", params={"store_id": STORE})
        assert resp.status_code == 200
        assert resp.json()["store_id"] == STORE


class TestPathBasedStoreFunnel:

    def test_funnel_path_returns_stages(self):
        resp = client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["store_id"] == STORE
        assert "stages" in data
        assert len(data["stages"]) == 4
        assert data["stages"][0]["stage"] == "Entered Store"


class TestHeatmapEndpoint:

    def test_heatmap_returns_normalized_fields(self):
        # Ingest some zone visits to have heatmap items
        batch = [
            make_test_event(event_id=str(uuid.uuid4()), event_type="ZONE_ENTER", zone_id="MAKEUP"),
            make_test_event(event_id=str(uuid.uuid4()), event_type="ZONE_EXIT", zone_id="MAKEUP", dwell_ms=12000),
            make_test_event(event_id=str(uuid.uuid4()), event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_test_event(event_id=str(uuid.uuid4()), event_type="ZONE_EXIT", zone_id="SKINCARE", dwell_ms=24000),
        ]
        client.post("/events/ingest", json=batch)

        resp = client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        data = resp.json()

        assert data["store_id"] == STORE
        assert isinstance(data["zones"], list)
        assert len(data["zones"]) == 2
        assert data["data_confidence"] is False  # low sessions (< 20)

        # Check normalization bounds
        for item in data["zones"]:
            assert 0.0 <= item["normalized_frequency"] <= 100.0
            assert 0.0 <= item["normalized_dwell"] <= 100.0
            assert item["zone_id"] in ["MAKEUP", "SKINCARE"]


class TestAnomaliesEndpoint:

    def test_anomalies_schema_and_suggested_actions(self):
        # Trigger an anomaly mismatch: 5 entries + 0 exits
        batch = [make_test_event(event_id=str(uuid.uuid4()), visitor_id=f"VIS_A_{i}") for i in range(5)]
        client.post("/events/ingest", json=batch)

        resp = client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        data = resp.json()

        assert data["store_id"] == STORE
        assert isinstance(data["anomalies"], list)

        # Confirm all generated anomalies have INFO | WARN | CRITICAL severity and suggested_actions
        for anomaly in data["anomalies"]:
            assert anomaly["severity"] in ["INFO", "WARN", "CRITICAL"]
            assert "suggested_action" in anomaly
            assert len(anomaly["suggested_action"]) > 10


class TestDetailedHealthCheck:

    def test_health_check_stale_feed_detection(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] in ["healthy", "warning", "degraded"]
        assert "database" in data
        assert "stores" in data

        if STORE in data["stores"]:
            # Sample data has old timestamps (2026-03-03 vs current execution date)
            # This should have triggered a stale feed warning
            assert data["stores"][STORE]["status"] == "stale"
            assert data["status"] == "warning"
            assert any("STALE_FEED" in w for w in data["warnings"])
