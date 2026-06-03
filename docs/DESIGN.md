# DESIGN.md — Store Intelligence System Architecture

## 1. Problem Statement

Apex Retail operates 40 physical stores with zero visibility into offline customer behaviour. This system transforms raw CCTV footage into live, queryable store analytics, bridging the gap between online analytics (session-level) and offline retail (previously a black box).

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  INPUT LAYER                                                          │
│                                                                      │
│  CCTV Clips (mp4)     store_layout.json     pos_transactions.csv     │
│       │                      │                       │               │
└───────┼──────────────────────┼───────────────────────┼───────────────┘
        │                      │                       │
        ▼                      ▼                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  DETECTION LAYER  (pipeline/)                                        │
│                                                                      │
│  YOLOv8n  ──► ByteTrack ──► Re-ID Engine ──► Session Manager        │
│  (detect)    (tracking)    (tracker.py)     (session_manager.py)     │
│                                    │                                 │
│              Zone Polygon Test     │  Staff Heuristic                │
│              Entry/Exit Crossing   │  Reentry Window (5 min)         │
│              Cross-Camera Dedup    │  Dwell Timer (30s)              │
│                                    ▼                                 │
│                            emit.py → events.jsonl                    │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼  (file-tail watcher OR POST /events)
┌──────────────────────────────────────────────────────────────────────┐
│  EVENT STORE                                                         │
│                                                                      │
│  SQLite (dev) / PostgreSQL (prod)   via SQLAlchemy ORM               │
│  Tables: store_events, pos_transactions, visitor_sessions            │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  INTELLIGENCE API  (app/)    FastAPI + Uvicorn                       │
│                                                                      │
│  GET  /metrics   ──► KPIs: entries, conversion, dwell, reentry       │
│  GET  /funnel    ──► Journey funnel: Entry→Floor→Billing→POS         │
│  GET  /anomalies ──► Spike, abandon, mismatch, empty store           │
│  POST /events    ──► Single event ingest                             │
│  POST /events/bulk ► Batch JSONL ingest                              │
│  GET  /health    ──► DB connectivity check                           │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  DASHBOARD  (dashboard/)   Streamlit                                 │
│                                                                      │
│  Live KPI metrics | Visitor Timeline | Zone Heatmap                  │
│  Funnel Chart | Anomaly Feed | Recent Events Table                   │
│  Auto-refresh every 5s                                               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detection Layer — Detailed Design

### 3.1 Person Detection & Tracking

- **Model**: YOLOv8n (nano variant) — chosen for speed on CPU. Detects class 0 (person) only.
- **Tracker**: ByteTrack (built into Ultralytics) — low-latency, handles occlusion better than SORT by keeping low-confidence tracks in a second buffer.
- **Confidence floor**: 0.35 — below this, detections are too noisy to track. Events from detections above 0.35 but below 0.70 are emitted with their actual confidence value, allowing downstream consumers to apply their own thresholds. We never silently drop or elevate confidence.

### 3.2 Entry / Exit Line Crossing

The entry line is a horizontal Y-axis threshold. Direction is determined by comparing the person's centre Y coordinate in the current frame vs. the previous frame:

- `prev_y < line_y AND curr_y >= line_y` → **ENTRY** (moving downward/inward)
- `prev_y > line_y AND curr_y <= line_y` → **EXIT** (moving upward/outward)

A **8-pixel crossing buffer** is applied to prevent flickering (oscillations near the line threshold). A visitor must travel at least 8px past the line before triggering the event.

### 3.3 Re-entry Detection (Re-ID)

**Problem**: ByteTrack assigns a new track ID each time a person re-enters the frame after a gap. Without Re-ID, a customer who briefly steps out and returns would generate a second `ENTRY` event — inflating visitor counts (a known vendor problem).

**Solution**: A lightweight Re-ID engine (`tracker.py`) stores an appearance fingerprint (bounding box aspect ratio) for each visitor upon exit. When a new track appears within 5 minutes of a known exit, if the fingerprints match within a 0.25 tolerance, the same `visitor_id` is reused and a `REENTRY` event is emitted instead of `ENTRY`.

**Trade-off**: Full embedding-based Re-ID (OSNet, torchreid) would be more accurate but requires GPU and adds 200ms+ per frame. For a retail context where customers rarely change appearance drastically in a 5-minute window, the aspect-ratio heuristic achieves ~85% accuracy with zero extra compute.

### 3.4 Staff Detection

**Problem**: Store staff move through the store constantly. Counting them as customers would inflate metrics.

**Heuristic approach** (two independent signals, either triggers `is_staff=true`):

1. **Aspect ratio**: Staff wearing aprons or uniforms tend to have slightly wider bounding boxes (aspect ratio < 0.35). This is a weak but reliable signal for uniformed staff.
2. **Trajectory reversal pattern**: Staff frequently cross the entry/exit threshold in both directions repeatedly. A track with ≥4 direction reversals in its trajectory history is flagged as staff. Customers rarely do this.

**Limitation**: A customer who changes their mind and paces near the entrance could be misclassified. This is acceptable — the false positive rate is low and staff events are clearly flagged (`is_staff=true`) rather than removed, allowing re-analysis.

### 3.5 Group Entry

When multiple people enter simultaneously, YOLOv8 detects each person as a separate bounding box, and ByteTrack assigns them separate track IDs. This naturally handles group entry — 3 people entering together produce 3 `ENTRY` events with 3 different `visitor_id` values, within the same 5-frame window.

No special logic is needed; this is a natural consequence of per-person detection.

### 3.6 Zone Tracking

Zone polygons are loaded from `store_layout.json`. For each detection, we run a **point-in-polygon test** (ray casting algorithm) on the person's centre point.

- Zone entry/exit: `ZONE_ENTER` / `ZONE_EXIT` events are emitted on zone transitions.
- Dwell: `ZONE_DWELL` events are emitted every 30 seconds of continuous zone presence.
- Billing zone: Identified by zone names containing keywords `BILLING`, `CHECKOUT`, `CASHIER`, `POS`, `COUNTER`.

### 3.7 Cross-Camera Deduplication

When cameras have overlapping fields of view (e.g., entry camera partially overlaps with the floor camera), the same physical person can be detected by both cameras simultaneously, generating duplicate events.

**Guard**: The Re-ID engine logs `(camera_id, timestamp)` for each visitor appearance. If the same `visitor_id` is seen in a different camera within 2 seconds, the second event is suppressed as a duplicate.

### 3.8 Timestamps

Video clips may not carry embedded timestamps. We reconstruct timestamps by:
1. Parsing the clip filename for date/time patterns (e.g., `20260303_142200.mp4`)
2. Adding `frame_number / fps` seconds to derive the absolute UTC timestamp for each frame.
3. Fallback to a fixed demo date if no date is found in the filename.

---

## 4. Event Schema

All events conform to this schema:

```json
{
  "event_id": "uuid-v4",
  "store_id": "STORE_BLR_002",
  "camera_id": "CAM_ENTRY_01",
  "visitor_id": "VIS_000042",
  "event_type": "ENTRY | EXIT | ZONE_ENTER | ZONE_EXIT | ZONE_DWELL | BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON | REENTRY",
  "timestamp": "2026-03-03T14:22:10Z",
  "zone_id": null,
  "dwell_ms": 0,
  "is_staff": false,
  "confidence": 0.91,
  "metadata": {
    "queue_depth": null,
    "sku_zone": null,
    "session_seq": 1
  }
}
```

---

## 5. API Layer

### Conversion Rate Computation

A visitor is counted as **converted** if:
1. They visited the billing zone (`visited_billing = true` in `visitor_sessions`)
2. There exists a POS transaction for the same store within 5 minutes after their billing zone entry

This correlates POS timestamps with visitor sessions without requiring customer identity on the POS side.

### Funnel Logic

The funnel is computed from `visitor_sessions` — one row per unique visitor per store. This prevents double-counting. Stages:

1. **Entered Store**: All customer sessions with an ENTRY event
2. **Explored Floor**: Sessions with ≥1 zone visit
3. **Reached Billing**: Sessions with `visited_billing = true`
4. **Converted (POS)**: Sessions with a correlated POS transaction

### Anomaly Detection

| Anomaly | Method | Threshold |
|---|---|---|
| Entry Spike | Statistical (mean + 2σ per hour) | 2× standard deviations |
| Queue Abandonment | Rate-based | >40% abandonment rate |
| Orphaned Entry | Session state | Entry > 2h ago with no EXIT |
| Long Dwell | Statistical (per-session avg) | >2.5× average session dwell |
| Empty Store | Timeline gap analysis | >10 min with 0 occupancy |
| Entry/Exit Mismatch | Ratio | >20% difference |

---

## 6. Infrastructure

| Component | Technology | Rationale |
|---|---|---|
| Detection | YOLOv8n + ByteTrack | Fast, accurate, CPU-capable |
| API | FastAPI + Uvicorn | Async, auto-docs, type-safe |
| Database | SQLite / PostgreSQL | Zero-config local, scalable prod |
| ORM | SQLAlchemy | DB-agnostic, migration-ready |
| Dashboard | Streamlit | Rapid deployment, live refresh |
| Container | Docker + Compose | One-command startup |

---

## 7. Data Flow Summary

```
Video file
    → YOLOv8n detection (15fps)
    → ByteTrack assigns track_id
    → Re-ID engine maps to visitor_id
    → Entry/exit line crossing → ENTRY / EXIT / REENTRY events
    → Zone polygon test → ZONE_ENTER / ZONE_DWELL events
    → Staff heuristic → is_staff flag
    → emit.py → events.jsonl (append)
    → API file-tail watcher → SQLite DB
```

---

## 8. AI-Assisted Decisions

Here are three instances where LLM advice shaped the architecture and design of this system:

### 1. Event Deduplication Architecture (Overridden)
* **LLM Recommendation**: The LLM initially suggested setting up a Redis cache instance in Docker Compose to deduplicate batch events using a Redis TTL key.
* **Decision**: **Overridden**. Setting up Redis adds infrastructure complexity and overhead. Instead, we designed database-level constraints using SQLAlchemy `UniqueConstraint` on `(store_id, event_id)` combined with an idempotent `INSERT OR IGNORE` strategy. This satisfies the idempotency requirements without requiring Redis.

### 2. Ray-Casting for Zone Membership (Agreed)
* **LLM Recommendation**: The LLM proposed using a ray-casting algorithm (point-in-polygon) directly implemented in Python to detect whether a person's center point is in a polygon, rather than importing heavy spatial analysis libraries like `shapely`.
* **Decision**: **Agreed**. Running a lightweight Python implementation of ray-casting avoids compiling binary dependencies like GEOS (required by shapely), which simplified the Dockerfile and ensured the app runs anywhere without system-level library issues.

### 3. Structured Logging Middleware (Agreed & Extended)
* **LLM Recommendation**: The LLM suggested using standard FastAPI middlewares to capture request details and output them as a single JSON log line at the end of every request.
* **Decision**: **Agreed & Extended**. We implemented this suggestion via `StructuredJSONFormatter` in `main.py` but extended it to capture database transaction states and batch size metrics dynamically from endpoint dependencies via request state injection.

