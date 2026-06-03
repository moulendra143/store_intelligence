# Store Intelligence API

Real-time retail analytics API that converts raw CCTV detection events into actionable store KPIs — visitor counts, conversion funnels, zone heatmaps, and operational anomaly detection.

---

## Quick Start (5 commands)

```bash
git clone <repo-url> store-intelligence
cd store-intelligence
cp data/events.jsonl.example data/events.jsonl 2>/dev/null || true   # optional: seed demo data
docker compose up --build
```

The API is now live at **http://localhost:8000**.  
Interactive API docs: **http://localhost:8000/docs**
Live Real-Time Dashboard: **http://localhost:8501**

> If you have pre-existing `data/events.jsonl` the API loads it automatically on startup.

---

## Architecture

```
CCTV Clips
    │
    ▼
pipeline/run.py  (YOLOv8 detection + ReID tracking)
    │  emits JSONL events
    ▼
POST /events/ingest  ──► SQLite / PostgreSQL
    │
    ├── GET /stores/{id}/metrics    Store KPIs
    ├── GET /stores/{id}/funnel     Visitor journey
    ├── GET /stores/{id}/heatmap    Zone dwell heatmap
    ├── GET /stores/{id}/anomalies  Operational anomalies
    └── GET /health                 Service health
```

---

## Running the Detection Pipeline Against Video Clips

### Locally (Python environment)

```bash
# 1. Create a virtual environment
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Place your video clips in data/
#    Supported formats: .mp4 .avi .mov .mkv
#    Naming hints:  *entry* → CAM_ENTRY_01,  *floor* → CAM_FLOOR_01,  *billing* → CAM_BILLING_01

# 4. Run the API (in one terminal)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 5. Run the detection pipeline (in another terminal)
python pipeline/run.py \
  --store STORE_BLR_002 \
  --data-dir data \
  --output data/events.jsonl \
  --post-to-api http://localhost:8000
```

Events are streamed to the API in real time. The file-watcher also picks up any new lines written to `data/events.jsonl` automatically.

### Via Docker Compose (pipeline + API together)

Uncomment the `pipeline` service in `docker-compose.yml`, then:

```bash
# Place video clips in ./data/ first
docker compose up --build
```

The pipeline container waits for the API health-check to pass before starting.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/events/ingest` | Ingest a batch of ≤ 500 detection events. Idempotent by `event_id`. |
| `GET` | `/stores/{store_id}/metrics` | Real-time KPIs: entries, conversion rate, dwell, queue depth |
| `GET` | `/stores/{store_id}/funnel` | Session-based visitor journey funnel |
| `GET` | `/stores/{store_id}/heatmap` | Normalised zone dwell & frequency heatmap |
| `GET` | `/stores/{store_id}/anomalies` | Detected operational anomalies with severity & suggested actions |
| `GET` | `/health` | Service health, per-store last-event lag, stale-feed detection |
| `GET` | `/events` | Query raw events with filters |

### Example: ingest a batch

```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '[
    {
      "event_id": "evt-001",
      "store_id": "STORE_BLR_002",
      "camera_id": "CAM_ENTRY_01",
      "visitor_id": "VIS_0001",
      "event_type": "ENTRY",
      "timestamp": "2026-03-03T09:00:00Z",
      "zone_id": null,
      "dwell_ms": 0,
      "is_staff": false,
      "confidence": 0.92,
      "metadata": {}
    }
  ]'
```

### Example: get store metrics

```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

---

## Running Tests

```bash
# Run all tests
pytest -v

# Run with coverage report
pytest --cov=app --cov-report=term-missing

# Run only Part C production-readiness tests
pytest tests/test_part_c.py -v
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./data/store_intelligence.db` | SQLAlchemy connection string. Use `postgresql://...` for production. |
| `EVENTS_FILE` | `data/events.jsonl` | Path to detection events JSONL file loaded on startup. |
| `POS_FILE` | `data/pos_transactions.csv` | Path to POS transactions CSV loaded on startup. |

---

## Structured Logging

Every request emits one JSON log line containing:

```json
{
  "ts": "2026-03-03T09:00:01.234Z",
  "level": "INFO",
  "logger": "store_intelligence",
  "message": "request completed",
  "trace_id": "3f2a1b4c-...",
  "store_id": "STORE_BLR_002",
  "endpoint": "/stores/STORE_BLR_002/metrics",
  "method": "GET",
  "latency_ms": 12,
  "status_code": 200,
  "event_count": 47
}
```

`event_count` is only present on `POST /events/ingest` requests.

---

## Graceful Degradation

- **DB unavailable**: returns `HTTP 503` with structured JSON body — no raw stack traces.
- **Unknown store**: returns all-zero metrics / empty funnel — never 500.
- **Malformed events**: partial success — valid events in a batch are still ingested.

---

## Event Schema

```json
{
  "event_id":   "string (UUID, unique)",
  "store_id":   "string",
  "camera_id":  "string",
  "visitor_id": "string",
  "event_type": "ENTRY | EXIT | REENTRY | ZONE_ENTER | ZONE_EXIT | ZONE_DWELL | BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON | STAFF_ENTRY | STAFF_EXIT",
  "timestamp":  "ISO-8601 string (UTC)",
  "zone_id":    "string | null",
  "dwell_ms":   "integer (milliseconds)",
  "is_staff":   "boolean",
  "confidence": "float [0.0–1.0]",
  "metadata":   { "queue_depth": "int|null", "sku_zone": "string|null", "session_seq": "int|null" }
}
```
