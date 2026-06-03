# Store Intelligence API

Real-time retail analytics API that converts raw CCTV detection events into actionable store KPIs — visitor counts, conversion funnels, zone heatmaps, and operational anomaly detection.

---

## ⚠️ Dataset Notice

**The dataset files are NOT included in this repository** (per challenge licensing terms — clips must not be published or redistributed).

You will need to obtain the dataset ZIP from the challenge organiser and place the files as described in the [Dataset Setup](#dataset-setup) section below before running the pipeline.

---

## Quick Start (5 commands)

```bash
# 1. Clone the repo                                    (~5 seconds)
git clone <repo-url> store-intelligence
cd store-intelligence

# 2. Place your dataset files in data/
#    (see Dataset Setup section below — no data files are committed to the repo)

# 3. Build & start the API + Dashboard                (~3–6 min first build, ~20s cached)
docker compose up --build
```

The API is now live at **http://localhost:8000**  
Interactive API docs: **http://localhost:8000/docs**  
Live Real-Time Dashboard: **http://localhost:8501**

> The API automatically loads `data/events.jsonl` and `data/pos_transactions.csv` on startup if they exist.

---

## Docker Build Time Breakdown

The first `docker compose up --build` is the slowest step because it installs all dependencies inside the image. Subsequent runs use Docker's layer cache and are near-instant.

| Step | First Run | Cached Run | Notes |
|------|-----------|------------|-------|
| Pull `python:3.11-slim` base image | ~30 s | 0 s | ~150 MB pull |
| Install system packages (`gcc`, `curl`) | ~20 s | 0 s | Cached as a layer |
| `pip install requirements-api.txt` | ~40 s | 0 s | FastAPI, SQLAlchemy, Pydantic |
| `pip install requirements-dashboard.txt` | ~60 s | 0 s | Streamlit, Plotly, Pandas |
| `pip install requirements-pipeline.txt` | ~90–120 s | 0 s | Ultralytics YOLOv8 + PyTorch (~2.5 GB) |
| Copy application source | ~2 s | ~2 s | Only changes if code changed |
| API container cold start | ~5 s | ~5 s | DB init + POS/events file load |
| **Total first build** | **~3–6 min** | **~20–30 s** | Depends on internet speed |

> **Tip:** If you only need the API and Dashboard (no pipeline), you can skip the heavy pipeline deps by removing `requirements-pipeline.txt` from the Dockerfile `pip install` step — reducing build time to under 2 minutes.

---

## Dataset Setup

After cloning, extract the challenge dataset ZIP and copy the files into `data/` as follows:

```
store_intelligence/
└── data/
    ├── store_layout.json          ← Zone definitions for each store
    ├── pos_transactions.csv       ← Timestamped POS transaction records
    ├── sample_events.jsonl        ← 200 example events (for pipeline validation)
    └── videos/                    ← Place CCTV clips here
        ├── store_blr_002_entry.mp4
        ├── store_blr_002_floor.mp4
        └── store_blr_002_billing.mp4
```

### File descriptions

| File | Source | Description |
|------|--------|-------------|
| `store_layout.json` | Challenge ZIP | Zone polygon definitions, camera IDs, open hours |
| `pos_transactions.csv` | Challenge ZIP | `store_id, transaction_id, timestamp, basket_value_inr` |
| `sample_events.jsonl` | Challenge ZIP | 200 reference events to validate your detection output |
| `data/videos/*.mp4` | Challenge ZIP | CCTV clips — Entry, Main Floor, Billing (1080p, 15fps, ~20 min each) |

### Video naming convention

The pipeline auto-assigns `camera_id` based on the filename. Name your clips accordingly:

| Filename keyword | Assigned `camera_id` |
|-----------------|----------------------|
| `*entry*` | `CAM_ENTRY_01` |
| `*floor*` | `CAM_FLOOR_01` |
| `*billing*` | `CAM_BILLING_01` |

Example: `store_blr_002_entry_camera.mp4` → `CAM_ENTRY_01`

---

## Architecture

```
data/videos/*.mp4   data/store_layout.json   data/pos_transactions.csv
        │                      │                          │
        ▼                      │                          │
pipeline/run.py                │                          │
(YOLOv8n + ByteTrack ReID)     │                          │
        │ emits JSONL events   │                          │
        ▼                      ▼                          ▼
POST /events/ingest  ──────► SQLite DB ◄─── startup loader
        │
        ├── GET /stores/{id}/metrics     Real-time KPIs
        ├── GET /stores/{id}/funnel      Visitor journey funnel
        ├── GET /stores/{id}/heatmap     Zone dwell heatmap
        ├── GET /stores/{id}/anomalies   Operational anomalies
        └── GET /health                  Service health + stale-feed
                │
                ▼
        Streamlit Dashboard (port 8501)
```

---

## Running the Detection Pipeline Against Video Clips

### Option A — Locally (Python environment)

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. Install pipeline dependencies
pip install -r requirements-pipeline.txt

# 3. Make sure your video clips are in data/videos/
#    and store_layout.json is in data/

# 4. Start the API in one terminal
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 5. Run the detection pipeline in a second terminal
python pipeline/run.py \
  --store STORE_BLR_002 \
  --data-dir data \
  --output data/events.jsonl \
  --post-to-api http://localhost:8000
```

Events are written to `data/events.jsonl` and simultaneously POSTed to the API in real time. The API's background file-watcher also picks up any new lines automatically.

### Option B — Docker Compose (pipeline + API + Dashboard together)

1. Place your video clips in `./data/videos/`
2. Uncomment the `pipeline` service block in `docker-compose.yml`
3. Run:

```bash
docker compose up --build
```

The pipeline container waits for the API health-check to pass before it starts processing.

### Pipeline output

Each processed clip emits structured JSONL events to `data/events.jsonl`:

```jsonl
{"event_id":"uuid-v4","store_id":"STORE_BLR_002","camera_id":"CAM_ENTRY_01","visitor_id":"VIS_000001","event_type":"ENTRY","timestamp":"2026-03-03T09:04:11Z","zone_id":null,"dwell_ms":0,"is_staff":false,"confidence":0.91,"metadata":{"queue_depth":null,"sku_zone":null,"session_seq":1}}
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/events/ingest` | Ingest a batch of ≤ 500 detection events. Idempotent by `event_id`. Partial success on malformed events. |
| `GET` | `/stores/{store_id}/metrics` | Real-time KPIs: unique visitors, conversion rate, avg dwell per zone, queue depth, abandonment rate |
| `GET` | `/stores/{store_id}/funnel` | Session-based visitor journey: Entry → Floor → Billing → Purchase with drop-off % |
| `GET` | `/stores/{store_id}/heatmap` | Zone visit frequency + avg dwell, normalised 0–100 |
| `GET` | `/stores/{store_id}/anomalies` | Active anomalies with INFO / WARN / CRITICAL severity and `suggested_action` |
| `GET` | `/health` | Service status, last event timestamp per store, STALE_FEED warning if > 10 min lag |
| `GET` | `/events` | Query raw events with filters (store_id, event_type, visitor_id, time range) |

### Example — ingest a batch

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
      "metadata": {"queue_depth": null, "sku_zone": null, "session_seq": 1}
    }
  ]'
```

### Example — get store metrics

```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

### Example — load the sample events file for instant demo

```bash
# With the API running, bulk-load sample_events.jsonl to see all endpoints working immediately:
curl -X POST "http://localhost:8000/events/bulk?filepath=data/sample_events.jsonl"
```

---

## Running Tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

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
| `DATABASE_URL` | `sqlite:///./data/store_intelligence.db` | SQLAlchemy connection string. Use `postgresql://user:pass@host/db` for production. |
| `EVENTS_FILE` | `data/events.jsonl` | Path to detection events JSONL file — loaded on startup and tailed continuously. |
| `POS_FILE` | `data/pos_transactions.csv` | Path to POS transactions CSV — loaded on startup for conversion correlation. |
| `API_URL` | `http://localhost:8000` | API base URL used by the Streamlit dashboard. |
| `STORE_ID` | `STORE_BLR_002` | Default store ID shown in the dashboard. |

---

## Structured Logging

Every request emits one JSON log line:

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

| Failure | Behaviour |
|---------|-----------|
| DB unavailable | HTTP 503 with structured JSON — no raw stack traces |
| Unknown store ID | All-zero metrics / empty funnel — never a 500 |
| Malformed events in batch | Partial success — valid events are still ingested; errors reported per-event |
| API unreachable (dashboard) | Dashboard falls back to reading `data/events.jsonl` directly |

---

## Event Schema

```json
{
  "event_id":   "string (UUID v4 — globally unique)",
  "store_id":   "string  (e.g. STORE_BLR_002)",
  "camera_id":  "string  (e.g. CAM_ENTRY_01)",
  "visitor_id": "string  (e.g. VIS_000001  — stable per visit session)",
  "event_type": "ENTRY | EXIT | REENTRY | ZONE_ENTER | ZONE_EXIT | ZONE_DWELL | BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON",
  "timestamp":  "ISO-8601 UTC  (e.g. 2026-03-03T14:22:10Z)",
  "zone_id":    "string | null",
  "dwell_ms":   "integer  (milliseconds; 0 for instantaneous events)",
  "is_staff":   "boolean",
  "confidence": "float [0.0–1.0]  (never suppressed — low-conf events are emitted)",
  "metadata": {
    "queue_depth":   "integer | null  (populated for BILLING_QUEUE_JOIN)",
    "sku_zone":      "string  | null  (zone label from store_layout.json)",
    "session_seq":   "integer         (ordinal position of event in visitor session)"
  }
}
```

---

## Repository Structure

```
store_intelligence/
├── pipeline/
│   ├── detect.py          # YOLOv8 detection + zone tracking + staff heuristic
│   ├── tracker.py         # Re-ID engine + cross-camera deduplication
│   ├── session_manager.py # Per-visitor session state machine
│   ├── emit.py            # Event schema creation + JSONL / API emission
│   └── run.py             # Entry point — processes all clips for a store
├── app/
│   ├── main.py            # FastAPI app, middleware, structured logging
│   ├── models.py          # SQLAlchemy ORM + Pydantic schemas
│   ├── database.py        # DB engine, session factory, 503 guard
│   ├── ingestion.py       # POST /events/ingest, file-tail watcher
│   ├── metrics.py         # GET /stores/{id}/metrics
│   ├── funnel.py          # GET /stores/{id}/funnel
│   ├── heatmap.py         # GET /stores/{id}/heatmap
│   ├── anomalies.py       # GET /stores/{id}/anomalies
│   └── health.py          # GET /health
├── dashboard/
│   └── app.py             # Streamlit live dashboard (port 8501)
├── tests/
│   ├── test_ingestion.py  # Ingestion endpoint tests (idempotency, schema)
│   ├── test_metrics.py    # Metrics endpoint tests
│   ├── test_anomalies.py  # Anomaly detection tests
│   ├── test_part_b.py     # Full Part B API contract tests
│   └── test_part_c.py     # Part C production-readiness tests
├── data/                  # ← NOT in repo — see Dataset Setup above
│   ├── store_layout.json
│   ├── pos_transactions.csv
│   ├── sample_events.jsonl
│   └── videos/
├── Dockerfile
├── docker-compose.yml
├── DESIGN.md
├── CHOICES.md
└── README.md
```

> **Note:** The `data/` directory is excluded from version control. All dataset files must be sourced from the challenge organiser.
