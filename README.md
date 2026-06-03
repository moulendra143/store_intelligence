# Store Intelligence: Real-Time Retail CCTV KPI Pipeline & API

Real-time retail analytics platform that converts raw CCTV video streams into actionable store KPIs. By running YOLOv8 detection, multi-camera Re-ID tracking, and store zone boundary heuristics, the pipeline extracts visitor footprints and matches them against Point of Sale (POS) records to calculate conversion funnels, dwell heatmaps, and operational anomalies.

---

## Problem Statement

Traditional brick-and-mortar retail suffers from a lack of visual funnel analytics. While online e-commerce platforms can track user clickthrough rates, cart abandonment, and page heatmaps, offline stores have historically been limited to daily sales totals. 

This project bridges that gap by using existing CCTV camera feeds (Entry, Main Floor, and Billing) to:
1. **Differentiate** between customers and store staff.
2. **Track** individual visitor journeys seamlessly across non-overlapping camera fields of view.
3. **Measure** zone-specific dwell times and identify queues or bottlenecks.
4. **Correlate** entry events with POS transaction timestamps to calculate actual visitor-to-purchase conversion rates.
5. **Detect** operational anomalies like queue abandonment, long checkout delays, or stale video feeds in real time.

---

## System Architecture

The project consists of three main components: a computer vision pipeline, a FastAPI backend with a SQLite database, and an interactive Streamlit dashboard.

```
                  [ Video Streams: Entry / Floor / Billing ]
                                      │
                                      ▼
                        +───────────────────────────+
                        │  pipeline/run.py          │
                        │  - YOLOv8 Object Detect   │
                        │  - ByteTrack Re-ID        │
                        │  - Polygon Zone Filtering │
                        │  - Staff Identification   │
                        +───────────────────────────+
                                      │
                                      │ Emits JSONL Events (POST)
                                      ▼
+───────────────────────────────────────────────────────────────────────────+
│                           FastAPI Web Server                              │
│                                                                           │
│  +─────────────────────────+                   +───────────────────────+  │
│  │   /events/ingest        │                   │   File-Tail Watcher   │  │
│  │   (Idempotent API)      │                   │   (Tails events.jsonl)│  │
│  +────────────┬────────────+                   +───────────┬───────────+  │
│               │                                            │              │
│               └─────────────► [ SQLite Database ] ◄────────┘              │
│                                     │                                     │
│                                     ├── GET /stores/{id}/metrics          │
│                                     ├── GET /stores/{id}/funnel           │
│                                     ├── GET /stores/{id}/heatmap          │
│                                     ├── GET /stores/{id}/anomalies        │
│                                     └── GET /health (Stale Feed Check)    │
+─────────────────────────────────────┬─────────────────────────────────────+
                                      │
                                      ▼
                        +───────────────────────────+
                        │    Streamlit Dashboard    │
                        │    - Real-Time KPIs       │
                        │    - Heatmaps & Funnels   │
                        │    - Live Alerts & Logs   │
                        +───────────────────────────+
```

---

## Dataset Setup

**The dataset files are NOT included in this repository** per challenge licensing terms. You must obtain the dataset ZIP from the challenge organizer and place the files in the `data/` directory.

### Directory Layout
Extract and structure your `data/` directory as follows:
```
store_intelligence/
└── data/
    ├── store_layout.json          ← Polygon coordinates for zone definitions
    ├── pos_transactions.csv       ← POS transaction records (timestamp, basket value)
    ├── sample_events.jsonl        ← 200 reference events for validation
    └── videos/                    ← Raw CCTV footage (.mp4 files)
        ├── store_blr_002_entry.mp4
        ├── store_blr_002_floor.mp4
        └── store_blr_002_billing.mp4
```

### Video File Naming & Camera Mapping
The detection pipeline automatically detects which camera a video belongs to based on keywords in its filename. Name your video files accordingly:

| Filename Keyword | Assigned `camera_id` | Purpose |
|------------------|----------------------|---------|
| `*entry*`        | `CAM_ENTRY_01`       | Captures store entry/exit to track total visitors. |
| `*floor*`        | `CAM_FLOOR_01`       | Tracks movement in specific zones (e.g. aisles, shelves). |
| `*billing*`      | `CAM_BILLING_01`     | Monitors checkout queue lines. |

---

## Running the Application

### Method 1: Using Docker (Recommended)
This method boots up the database, API, and live dashboard as microservices in containerized environments.

```bash
# 1. Clone the repository
git clone <repo-url> store-intelligence
cd store-intelligence

# 2. Place files into data/ as described in the Dataset Setup section.

# 3. Start all services using Docker Compose
docker compose up --build
```

- **FastAPI API Endpoint**: [http://localhost:8000](http://localhost:8000)
- **Swagger Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Live Streamlit Dashboard**: [http://localhost:8501](http://localhost:8501)

### Method 2: Normal Local Setup (Direct Clone)
This method runs the services directly in your host OS Python environment.

```bash
# 1. Clone the repository and navigate inside
git clone <repo-url> store-intelligence
cd store-intelligence

# 2. Set up and activate a Python virtual environment
python -m venv venv
# On Windows (PowerShell):
venv\Scripts\Activate.ps1
# On Linux/macOS:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the FastAPI API backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 5. (In a new terminal) Run the Streamlit Dashboard
streamlit run dashboard/app.py --server.port 8501
```

To run the pipeline and process the CCTV video files locally:
```bash
# Ensure virtual environment is active, then run:
python pipeline/run.py \
  --store STORE_BLR_002 \
  --data-dir data \
  --output data/events.jsonl \
  --post-to-api http://localhost:8000
```

---

## Timing and Build Breakdowns

### Docker Installation & Build Time Breakdown

The first run takes longer due to downloading base images and heavy Python packages (such as PyTorch and YOLOv8). Subsequent builds are cached and take under 30 seconds.

| Build/Run Step | Estimated First Run | Estimated Cached Run | Details |
|---|---|---|---|
| Pulling `python:3.11-slim` Image | ~30 seconds | 0 seconds | Base operating system layer |
| Installing OS Packages (`gcc`, `curl`) | ~20 seconds | 0 seconds | System level dependencies |
| API Package Installation (`pip`) | ~40 seconds | 0 seconds | FastAPI, SQLAlchemy, SQLite |
| Dashboard Package Installation (`pip`) | ~60 seconds | 0 seconds | Streamlit, Plotly, Pandas |
| Pipeline Dependencies (PyTorch + YOLOv8) | ~120 seconds | 0 seconds | Deep learning models (~2.5 GB) |
| Starting Application Container | ~5 seconds | ~5 seconds | Initializing database & syncing file-tail watchers |
| **Total Startup Time** | **~4.5 minutes** | **~25 seconds** | Overall pipeline, API, and Dashboard setup |

> [!TIP]
> If you only want to review or run the API and Dashboard (without running model inference on video files), you can skip installing the pipeline dependencies by commenting out `requirements-pipeline.txt` in the Dockerfile. This reduces the first-build time to under 2 minutes.

---

## API Reference

### 1. Ingest Batch Events
* **Endpoint**: `POST /events/ingest`
* **Description**: Receives a batch of visitor detection events (up to 500 at a time). It is fully idempotent by `event_id`.
* **Request Body**:
  ```json
  [
    {
      "event_id": "evt_abc123_xyz",
      "store_id": "STORE_BLR_002",
      "camera_id": "CAM_ENTRY_01",
      "visitor_id": "VIS_0001",
      "event_type": "ENTRY",
      "timestamp": "2026-03-03T09:00:00Z",
      "zone_id": null,
      "dwell_ms": 0,
      "is_staff": false,
      "confidence": 0.95,
      "metadata": {
        "queue_depth": null,
        "sku_zone": null,
        "session_seq": 1
      }
    }
  ]
  ```

### 2. Fetch Store Metrics
* **Endpoint**: `GET /stores/{store_id}/metrics`
* **Description**: Returns top-level store performance indicators (Total unique visitors, purchase conversion rates, dwell times, and queue abandonment rates).

### 3. Fetch Journey Funnel
* **Endpoint**: `GET /stores/{store_id}/funnel`
* **Description**: Provides step-by-step visitor traffic conversion analysis: `Entry` ➔ `Floor` ➔ `Billing` ➔ `Purchase`.

### 4. Fetch Zone Dwell Heatmap
* **Endpoint**: `GET /stores/{store_id}/heatmap`
* **Description**: Calculates visitation density and average dwell time per store layout polygon zone.

### 5. Fetch Anomalies
* **Endpoint**: `GET /stores/{store_id}/anomalies`
* **Description**: Detects active store anomalies including queue blockages, high visitor-to-staff ratios, or checkout delays.

### 6. Health & Stale Feed Check
* **Endpoint**: `GET /health`
* **Description**: In addition to service status, alerts if incoming events lag by more than 10 minutes (indicating camera or pipeline crash).

### Demo Quick-Load Endpoint
To quickly populate the dashboard without running the video pipeline, run:
```bash
curl -X POST "http://localhost:8000/events/bulk?filepath=data/sample_events.jsonl"
```

---

## Repository Structure

```
store_intelligence/
├── app/                  # FastAPI Application Source Code
│   ├── database.py       # DB engine, session factory, 503 database guard
│   ├── ingestion.py      # POST /events/ingest & JSONL file tail-watcher
│   ├── main.py           # FastAPI initialization, middleware, JSON logger
│   ├── models.py         # SQLAlchemy models and Pydantic validation schemas
│   ├── anomalies.py      # Real-time alert/anomaly checking rules
│   ├── funnel.py         # Journey funnel path processing logic
│   ├── heatmap.py        # Zone density and dwell metric endpoints
│   ├── metrics.py        # Store-wide statistics and conversion computations
│   └── health.py         # API health and pipeline stale-feed checker
├── dashboard/            # Streamlit Analytics Dashboard
│   └── app.py            # Streamlit dashboard layout and interactive plots
├── pipeline/             # YOLOv8 Vision Processing Pipeline
│   ├── detect.py         # CCTV YOLOv8 bounding box, zone tracking, staff heuristics
│   ├── tracker.py        # Object tracking and multi-camera Re-ID linking
│   ├── session_manager.py# Visitor event logic state-machine
│   ├── emit.py           # Structuring and posting events
│   └── run.py            # Script command line interface entrypoint
├── data/                 # Raw/Ref dataset (Excluded from Git)
├── tests/                # Automated integration and unit tests
├── Dockerfile            # Multi-stage production container build
├── docker-compose.yml    # Full service orchestration configuration
├── render.yaml           # Deployment blueprint configuration for Render.com
└── README.md             # Project documentation
```

---

## Deployment on Render

This project contains a `render.yaml` blueprint configuration for fully automated deployments on Render.com.

### Deployment Instructions
1. Push this repository to your GitHub account.
2. Log in to [Render Dashboard](https://dashboard.render.com).
3. Select **Blueprints** from the top navigation bar.
4. Click **New Blueprint Instance** and connect your GitHub repository.
5. Render will detect `render.yaml` and provision both:
   - `store-intelligence-api` (FastAPI backend service)
   - `store-intelligence-dashboard` (Streamlit frontend service pointing to the backend API)
6. Once deployed, upload your initial database/dataset files directly to the persistent mounts, or use the API's `/events/ingest` endpoint to feed the live analytics.

---

## Conclusion

This Retail Store Intelligence system demonstrates how standard CCTV infrastructure can be converted into a rich source of structured business intelligence. With production-ready features like database resilience, real-time ingestion, automatic event tailing, and comprehensive analytics, retail managers can gain the same depth of insights inside their physical stores as they do on e-commerce platforms.
