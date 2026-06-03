"""
ingestion.py — Event ingestion endpoints and background file-tail watcher.

Endpoints:
  POST /events          — ingest a single event
  POST /events/bulk     — ingest a JSONL file or batch list
  POST /pos/load        — load pos_transactions.csv into DB
  GET  /events          — query stored events with filters

Background task:
  Watches data/events.jsonl for new lines and ingests them automatically.
"""

import os
import csv
import json
import time
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError as SAOperationalError, IntegrityError
from pydantic import ValidationError

from app.database import get_db_safe as get_db
from app.models import (
    StoreEvent, POSTransaction, VisitorSession, EventInSchema,
    IngestBatchResponse, IngestResultItem
)


router = APIRouter(tags=["ingestion"])

# ------------------------------------------------------------------
# SINGLE EVENT INGEST
# ------------------------------------------------------------------

@router.post("/events", status_code=201)
def ingest_event(event: EventInSchema, db: Session = Depends(get_db)):
    """Ingest a single detection event."""
    try:
        ts = _parse_timestamp(event.timestamp)
        db_event = StoreEvent(
            event_id=event.event_id,
            store_id=event.store_id,
            camera_id=event.camera_id,
            visitor_id=event.visitor_id,
            event_type=event.event_type,
            timestamp=ts,
            zone_id=event.zone_id,
            dwell_ms=event.dwell_ms,
            is_staff=event.is_staff,
            confidence=event.confidence,
            event_metadata=event.metadata.model_dump() if event.metadata else {},
        )
        db.add(db_event)
        db.commit()
        _update_visitor_session(db, event, ts)
        return {"status": "ok", "event_id": event.event_id}
    except IntegrityError:
        db.rollback()
        # Duplicate event_id — idempotent
        return {"status": "duplicate", "event_id": event.event_id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# BATCH EVENT INGEST
# ------------------------------------------------------------------

@router.post("/events/ingest", status_code=201, response_model=IngestBatchResponse)
def ingest_batch(request: Request, events: list[dict], db: Session = Depends(get_db)):
    """
    Accepts batches of up to 500 events in the request body.
    Validates, deduplicates, stores.
    Idempotent by event_id.
    Partial success on malformed events.
    """
    if len(events) > 500:
        raise HTTPException(
            status_code=400,
            detail="Batch size exceeds the maximum limit of 500 events."
        )

    ingested_count = 0
    skipped_count = 0
    failed_count = 0
    results: list[IngestResultItem] = []

    for raw in events:
        event_id = raw.get("event_id") if isinstance(raw, dict) else None

        if not isinstance(raw, dict):
            failed_count += 1
            results.append(IngestResultItem(
                event_id=None,
                status="error",
                error="Event must be a JSON object"
            ))
            continue

        # 1. Pydantic schema validation
        try:
            event = EventInSchema(**raw)
        except ValidationError as ve:
            failed_count += 1
            err_msg = "; ".join([f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in ve.errors()])
            results.append(IngestResultItem(
                event_id=event_id,
                status="error",
                error=f"Validation failed: {err_msg}"
            ))
            continue

        # 2. Database saving with Savepoint for atomicity per event
        try:
            ts = _parse_timestamp(event.timestamp)
            with db.begin_nested():
                db_event = StoreEvent(
                    event_id=event.event_id,
                    store_id=event.store_id,
                    camera_id=event.camera_id,
                    visitor_id=event.visitor_id,
                    event_type=event.event_type,
                    timestamp=ts,
                    zone_id=event.zone_id,
                    dwell_ms=event.dwell_ms,
                    is_staff=event.is_staff,
                    confidence=event.confidence,
                    event_metadata=event.metadata.model_dump() if event.metadata else {},
                )
                db.add(db_event)
                db.flush()  # checks unique constraint
                _update_visitor_session(db, event, ts)

            ingested_count += 1
            results.append(IngestResultItem(
                event_id=event.event_id,
                status="ok"
            ))
        except IntegrityError:
            # begin_nested automatically rolled back to savepoint
            skipped_count += 1
            results.append(IngestResultItem(
                event_id=event.event_id,
                status="duplicate",
                error="Duplicate event_id detected"
            ))
        except Exception as e:
            failed_count += 1
            results.append(IngestResultItem(
                event_id=event.event_id,
                status="error",
                error=str(e)
            ))

    db.commit()

    # Expose event_count for structured request logger
    request.state.event_count = ingested_count

    if failed_count == 0:
        status = "success"
    elif ingested_count > 0:
        status = "partial_success"
    else:
        status = "error"

    return IngestBatchResponse(
        status=status,
        ingested_count=ingested_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        results=results
    )


# ------------------------------------------------------------------
# BULK INGEST
# ------------------------------------------------------------------

@router.post("/events/bulk", status_code=201)
def ingest_bulk(
    background_tasks: BackgroundTasks,
    filepath: str = Query(default="data/events.jsonl", description="Path to JSONL file to load"),
    db: Session = Depends(get_db),
):
    """
    Load all events from a JSONL file into the database.
    Skips duplicate event_ids (idempotent).
    """
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"File not found: {filepath}")

    inserted = 0
    skipped = 0

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                ts = _parse_timestamp(raw.get("timestamp", ""))
                db_event = StoreEvent(
                    event_id=raw["event_id"],
                    store_id=raw["store_id"],
                    camera_id=raw["camera_id"],
                    visitor_id=raw["visitor_id"],
                    event_type=raw["event_type"],
                    timestamp=ts,
                    zone_id=raw.get("zone_id"),
                    dwell_ms=raw.get("dwell_ms", 0),
                    is_staff=raw.get("is_staff", False),
                    confidence=raw.get("confidence", 0.0),
                    event_metadata=raw.get("metadata", {}),
                )
                db.add(db_event)
                db.flush()
                inserted += 1
            except IntegrityError:
                db.rollback()
                skipped += 1
            except Exception:
                db.rollback()
                skipped += 1

    db.commit()

    # Rebuild session summaries
    background_tasks.add_task(_rebuild_all_sessions, filepath)

    return {"status": "ok", "inserted": inserted, "skipped": skipped, "source": filepath}


# ------------------------------------------------------------------
# QUERY EVENTS
# ------------------------------------------------------------------

@router.get("/events")
def query_events(
    store_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    visitor_id: Optional[str] = Query(None),
    start: Optional[str] = Query(None, description="ISO-8601 start time"),
    end: Optional[str] = Query(None, description="ISO-8601 end time"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    """Query stored events with optional filters."""
    q = db.query(StoreEvent)
    if store_id:
        q = q.filter(StoreEvent.store_id == store_id)
    if event_type:
        q = q.filter(StoreEvent.event_type == event_type)
    if visitor_id:
        q = q.filter(StoreEvent.visitor_id == visitor_id)
    if start:
        q = q.filter(StoreEvent.timestamp >= _parse_timestamp(start))
    if end:
        q = q.filter(StoreEvent.timestamp <= _parse_timestamp(end))
    total = q.count()
    events = q.order_by(StoreEvent.timestamp).offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "events": [_event_to_dict(e) for e in events],
    }


# ------------------------------------------------------------------
# POS TRANSACTION LOADER
# ------------------------------------------------------------------

@router.post("/pos/load", status_code=201)
def load_pos_transactions(
    filepath: str = Query(default="data/pos_transactions.csv"),
    db: Session = Depends(get_db),
):
    """Load POS transactions from CSV into the database."""
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"File not found: {filepath}")

    inserted = 0
    skipped = 0

    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = _parse_timestamp(row["timestamp"].strip())
                txn = POSTransaction(
                    store_id=row["store_id"].strip(),
                    transaction_id=row["transaction_id"].strip(),
                    timestamp=ts,
                    basket_value_inr=float(row.get("basket_value_inr", 0.0)),
                )
                db.add(txn)
                db.flush()
                inserted += 1
            except IntegrityError:
                db.rollback()
                skipped += 1
            except Exception:
                db.rollback()
                skipped += 1

    db.commit()
    return {"status": "ok", "inserted": inserted, "skipped": skipped}


# ------------------------------------------------------------------
# BACKGROUND FILE-TAIL WATCHER
# ------------------------------------------------------------------

_watcher_thread: Optional[threading.Thread] = None
_watcher_running = False


def start_file_watcher(filepath: str = "data/events.jsonl", api_db_factory=None):
    """
    Start a background thread that tails the events JSONL file and
    ingests new lines as they are written by the detection pipeline.
    """
    global _watcher_thread, _watcher_running

    if _watcher_running:
        return

    _watcher_running = True

    def _watch():
        from app.database import SessionLocal
        last_pos = 0
        if os.path.exists(filepath):
            last_pos = os.path.getsize(filepath)

        while _watcher_running:
            try:
                if os.path.exists(filepath):
                    current_size = os.path.getsize(filepath)
                    if current_size > last_pos:
                        with open(filepath, encoding="utf-8") as f:
                            f.seek(last_pos)
                            new_lines = f.readlines()
                        last_pos = current_size

                        if new_lines:
                            db = SessionLocal()
                            try:
                                for line in new_lines:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        raw = json.loads(line)
                                        ts = _parse_timestamp(raw.get("timestamp", ""))
                                        db_event = StoreEvent(
                                            event_id=raw["event_id"],
                                            store_id=raw["store_id"],
                                            camera_id=raw["camera_id"],
                                            visitor_id=raw["visitor_id"],
                                            event_type=raw["event_type"],
                                            timestamp=ts,
                                            zone_id=raw.get("zone_id"),
                                            dwell_ms=raw.get("dwell_ms", 0),
                                            is_staff=raw.get("is_staff", False),
                                            confidence=raw.get("confidence", 0.0),
                                            event_metadata=raw.get("metadata", {}),
                                        )
                                        db.add(db_event)
                                        db.flush()
                                    except IntegrityError:
                                        db.rollback()
                                    except Exception:
                                        db.rollback()
                                db.commit()
                            finally:
                                db.close()
            except Exception as e:
                pass  # Watcher keeps running
            time.sleep(1.0)

    _watcher_thread = threading.Thread(target=_watch, daemon=True, name="events-file-watcher")
    _watcher_thread.start()


def stop_file_watcher():
    global _watcher_running
    _watcher_running = False


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def _parse_timestamp(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp to datetime (UTC)."""
    if not ts_str:
        return datetime.now(timezone.utc)
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return datetime.now(timezone.utc)


def _event_to_dict(event: StoreEvent) -> dict:
    return {
        "id": event.id,
        "event_id": event.event_id,
        "store_id": event.store_id,
        "camera_id": event.camera_id,
        "visitor_id": event.visitor_id,
        "event_type": event.event_type,
        "timestamp": event.timestamp.isoformat() + "Z" if event.timestamp else None,
        "zone_id": event.zone_id,
        "dwell_ms": event.dwell_ms,
        "is_staff": event.is_staff,
        "confidence": event.confidence,
        "metadata": event.event_metadata or {},
    }


def _update_visitor_session(db: Session, event: EventInSchema, ts: datetime):
    """
    Update or create the VisitorSession summary row for this visitor.
    Called after each event is ingested.
    """
    try:
        session_row = db.query(VisitorSession).filter_by(
            store_id=event.store_id, visitor_id=event.visitor_id
        ).first()

        if session_row is None:
            session_row = VisitorSession(
                store_id=event.store_id,
                visitor_id=event.visitor_id,
                is_staff=event.is_staff,
                zones_visited={},
            )
            db.add(session_row)

        if event.event_type == "ENTRY":
            if session_row.entry_time is None:
                session_row.entry_time = ts

        elif event.event_type in ("REENTRY",):
            session_row.reentry_count = (session_row.reentry_count or 0) + 1

        elif event.event_type == "EXIT":
            session_row.exit_time = ts
            if session_row.entry_time:
                entry_ts = session_row.entry_time
                exit_ts = ts
                if entry_ts.tzinfo is None and exit_ts.tzinfo is not None:
                    entry_ts = entry_ts.replace(tzinfo=timezone.utc)
                elif entry_ts.tzinfo is not None and exit_ts.tzinfo is None:
                    exit_ts = exit_ts.replace(tzinfo=timezone.utc)
                session_row.dwell_seconds = int(
                    (exit_ts - entry_ts).total_seconds()
                )

        elif event.event_type in ("ZONE_ENTER", "ZONE_DWELL", "BILLING_QUEUE_JOIN"):
            if event.zone_id:
                zones = session_row.zones_visited or {}
                zones[event.zone_id] = zones.get(event.zone_id, 0) + 1
                session_row.zones_visited = zones
            if event.event_type == "BILLING_QUEUE_JOIN" or (
                event.zone_id and any(
                    kw in (event.zone_id or "").upper()
                    for kw in ("BILLING", "CHECKOUT", "CASHIER", "POS", "COUNTER")
                )
            ):
                session_row.visited_billing = True

        if event.is_staff:
            session_row.is_staff = True

        db.commit()
    except Exception:
        db.rollback()


def _rebuild_all_sessions(filepath: str):
    """Rebuild all VisitorSession rows from the events file after bulk load."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        # Clear existing sessions
        db.query(VisitorSession).delete()
        db.commit()

        if not os.path.exists(filepath):
            return

        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    ts = _parse_timestamp(raw.get("timestamp", ""))
                    from app.models import EventInSchema, EventMetadataSchema
                    meta_raw = raw.get("metadata") or {}
                    event = EventInSchema(
                        event_id=raw["event_id"],
                        store_id=raw["store_id"],
                        camera_id=raw["camera_id"],
                        visitor_id=raw["visitor_id"],
                        event_type=raw["event_type"],
                        timestamp=raw.get("timestamp", ""),
                        zone_id=raw.get("zone_id"),
                        dwell_ms=raw.get("dwell_ms", 0),
                        is_staff=raw.get("is_staff", False),
                        confidence=raw.get("confidence", 0.0),
                        metadata=EventMetadataSchema(**meta_raw),
                    )
                    _update_visitor_session(db, event, ts)
                except Exception:
                    pass
    finally:
        db.close()
