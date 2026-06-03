# CHOICES.md — Engineering Decision Log

Every major decision in this system was made with a specific rationale. This document records the trade-offs so that reviewers can assess whether the reasoning is sound — not just whether the code works.

---

## 1. Detection Model: YOLOv8n vs Larger Models

**Decision**: YOLOv8n (nano)

**Options considered**:
- YOLOv8x (extra large) — higher mAP, slower
- YOLOv9 / RT-DETR — newer architectures, higher accuracy
- MediaPipe — faster on mobile/edge, less accurate for retail overhead views

**Rationale**:
- The bottleneck is not accuracy — the challenge explicitly states this is not a model-building exercise.
- YOLOv8n runs at ~30fps on CPU for 1080p with `track()`. Larger models would drop to 3-5fps, making real-time processing impractical without a GPU.
- CCTV retail footage is relatively simple: frontal/overhead angles, clear person shapes. The nano model achieves >80% mAP on COCO for persons — sufficient for counting accuracy within ±10%.
- `yolov8n.pt` is already in the repo (6.5MB). No download required.

**Accepted risk**: In dense crowd scenarios (>10 people in frame), occlusion can cause misses. This is documented in the confidence calibration approach.

---

## 2. Tracker: ByteTrack vs DeepSORT vs StrongSORT

**Decision**: ByteTrack

**Options considered**:
- DeepSORT — adds appearance embeddings, better Re-ID but 2-3× slower
- StrongSORT — even more sophisticated, requires a Re-ID model
- ByteTrack — pure motion-based, fast, built into Ultralytics

**Rationale**:
- ByteTrack's key advantage is its "two-stage association" — it keeps low-confidence detections in a secondary buffer and tries to re-associate them. This handles occlusion better than vanilla SORT.
- For retail entry/exit counting, track continuity within the frame is more important than cross-frame Re-ID. ByteTrack excels at the former.
- We handle cross-session Re-ID ourselves (see decision 3), so we don't need DeepSORT's appearance embedding for that purpose.

**Accepted risk**: ByteTrack can lose tracks during heavy occlusion (e.g., billing queue buildup). The session timeout mechanism (120s) handles abandoned tracks gracefully.

---

## 3. Re-ID Strategy: Aspect Ratio vs Full Embedding

**Decision**: Bounding-box aspect ratio fingerprint with 5-minute re-entry window

**Options considered**:
- OSNet / torchreid — full person Re-ID using learned embeddings
- Colour histogram matching — match by dominant clothing colour
- IoU trajectory — match by position trajectory continuity

**Rationale**:
- Full embedding Re-ID (OSNet) requires GPU inference or adds 200-500ms per frame on CPU. Unacceptable for a real-time system on standard retail hardware.
- Colour histograms work in controlled lighting but fail in retail with varied lighting conditions (which the challenge explicitly mentions).
- Aspect ratio is surprisingly stable per-person in overhead CCTV: a person's body proportions don't change between store exits and re-entries.
- The 5-minute re-entry window is intentionally narrow — customers who leave and return after lunch (>1hr) are genuinely new visits and should be counted as ENTRY not REENTRY.

**Accepted risk**: Two people of very similar build entering the same door sequentially could be matched incorrectly. Estimated false positive rate: <5% in retail contexts.

---

## 4. Staff Detection: Heuristic vs Classification Model

**Decision**: Two-signal heuristic (aspect ratio + trajectory reversal count)

**Options considered**:
- Train a binary classifier (staff vs. customer) — requires labeled data
- Colour segmentation for uniform detection — requires calibration per store
- Manual zone exclusion — mark staff entry point and exclude tracks from there

**Rationale**:
- We have no labeled training data for staff classification.
- A uniform colour classifier would require store-specific calibration (different retailers have different uniforms).
- The trajectory-reversal heuristic exploits a genuine behavioural difference: staff cross the entry threshold multiple times per shift; customers rarely do.
- Events are flagged `is_staff=true` rather than dropped — this preserves the data and allows manual review or model retraining later.

**Accepted risk**: A confused or indecisive customer might oscillate near the entrance and be flagged as staff. This is recoverable because `is_staff` is a filter, not a deletion.

---

## 5. Entry Line: Static Y-Threshold vs Camera Calibration

**Decision**: Static Y-threshold (auto-detected as 60% of frame height for entry cameras)

**Options considered**:
- Homography-based ground plane projection — map 2D pixel coordinates to real-world coordinates
- Learned entry zone from store_layout.json polygon intersection
- Manual configuration per camera

**Rationale**:
- Full camera calibration requires intrinsic/extrinsic parameters and a calibration target — not available from raw CCTV footage.
- The store_layout.json approach requires zone polygons to include the entry line — these may not always be present.
- For a 1080p CCTV camera positioned above the entrance door, the door threshold consistently appears at ~55-65% of frame height. This assumption holds for the vast majority of retail CCTV installations.
- The `--entry-line-y` flag allows manual override per camera.

**Accepted risk**: Unusual camera angles (very low mounting, extreme tilt) would require manual configuration. This is documented.

---

## 6. Database: SQLite vs PostgreSQL

**Decision**: SQLite by default, PostgreSQL available via `DATABASE_URL`

**Options considered**:
- PostgreSQL only — production-grade but requires a running server
- Redis — fast for counters, but no SQL query support
- In-memory dict — zero setup, lost on restart

**Rationale**:
- SQLite makes the system zero-dependency for reviewers: `pip install -r requirements.txt` + `uvicorn app.main:app` is enough to run. No Docker required for basic evaluation.
- SQLite handles up to ~50k inserts/sec — more than enough for a 15fps detection pipeline.
- The SQLAlchemy ORM is DB-agnostic. Switching to PostgreSQL requires only changing `DATABASE_URL`.
- The `docker-compose.yml` includes a commented PostgreSQL service for production deployments.

**Accepted risk**: SQLite is not suitable for multi-process concurrent writes (e.g., running 3 camera pipelines in parallel writing to the same file). For production, `DATABASE_URL=postgresql://...` should be set.

---

## 7. POS Correlation: 5-Minute Window vs Session Matching

**Decision**: 5-minute time window after billing zone entry

**Options considered**:
- Exact customer ID matching — not possible (no customer_id in POS data)
- Session overlap — mark as converted if visitor was in store during transaction
- Billing zone + time window — our approach

**Rationale**:
- The challenge specification explicitly states: "A visitor who was in the billing zone in the 5-minute window before a transaction timestamp counts as a converted visitor."
- We extend this to also look forward from the billing zone entry time (up to 5 minutes) to account for queue wait times.
- This is probabilistic, not deterministic — acknowledged in the conversion rate description.

**Accepted risk**: If multiple visitors are in the billing zone simultaneously, all of them may be counted as converted for a single transaction. This is an inherent limitation of the approach and disclosed in the API response.

---

## 8. Event Emission: Real-time POST vs File Append

**Decision**: File-append to JSONL + background file-tail watcher in API

**Options considered**:
- Real-time `POST /events` from pipeline — tight coupling, fails if API is down
- Message queue (Redis Streams, Kafka) — decoupled but adds infrastructure
- File append + watcher — decoupled, no extra infrastructure

**Rationale**:
- File append is the most resilient approach: the pipeline continues writing even if the API is restarting.
- The file-tail watcher in the API picks up new events within ~1 second.
- The pipeline also supports `--post-to-api` for direct HTTP delivery when real-time latency matters.
- For the evaluation context, starting with demo data generation → API load → query is zero-friction.

---

## 9. Cross-Camera Deduplication: 2-Second Window

**Decision**: Suppress events if same visitor_id seen in 2 cameras within 2 seconds

**Rationale**:
- Camera processing is frame-synchronised at 15fps. A 2-second window covers ~30 frames — sufficient to catch simultaneous detections from overlapping FOVs.
- The window is intentionally short (2s) to avoid suppressing legitimate events in cameras with non-overlapping coverage (e.g., an entry camera and a billing camera 30 metres apart).

---

## 10. Confidence Calibration: Floor vs Suppress

**Decision**: Emit all events above `min_confidence=0.35` with actual confidence values

**Rationale**:
- The challenge explicitly requires: "confidence calibration — are low-confidence detections flagged rather than silently dropped or falsely elevated?"
- We never substitute a different confidence value. The confidence emitted is exactly what YOLOv8 returned.
- Downstream consumers (API, analytics) can filter by confidence if needed. The raw signal is preserved.
- The 0.35 floor removes truly noisy detections (shadow regions, reflections) without being overly conservative.

---

## 11. Event Schema Design Rationale

**Decision**: Structured Flat Schema with nested Metadata dictionary.

**Options considered**:
- **Normalized relational tables**: Different tables for `EntryExitEvent`, `ZoneTransitionEvent`, etc.
- **Strictly flat string attributes**: A completely flat schema where all properties are key-value string/float pairs at the top level.
- **Hybrid schema**: Flat top-level fields for universal properties (`event_id`, `store_id`, `camera_id`, `visitor_id`, `event_type`, `timestamp`) and a flexible `metadata` dictionary for type-specific payloads.

**Rationale**:
- A single unified schema simplifies event ingestion and persistence. The API only needs a single ingestion endpoint (`POST /events/ingest`) rather than different endpoints for different event types.
- The top-level fields are guaranteed to be present for every single event.
- The `metadata` dict enables extensibility. For instance, `queue_depth` is only relevant for checkout zones, and `sku_zone` is only relevant for specific product interactions. A nested structure avoids polluting the database table with dozens of empty/null columns.
- What AI suggested: The AI suggested a highly normalized schema with separate database endpoints. We chose the hybrid/flat schema to minimize network roundtrips and keep database operations highly transactional.

