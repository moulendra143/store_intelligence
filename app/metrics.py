"""
metrics.py — /metrics endpoint.

Returns store-level business KPIs:
- Entry/exit counts
- Unique visitor count (customer sessions, excluding staff)
- Average dwell time
- Conversion rate (billing zone visits correlated with POS transactions)
- Re-entry rate
- Staff movements
- Currently inside count
- Peak hour
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, and_

from app.database import get_db_safe as get_db
from app.models import StoreEvent, POSTransaction, VisitorSession, MetricsResponse

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_model=MetricsResponse)
def get_metrics(
    store_id: str = Query(..., description="Store ID, e.g. STORE_BLR_002"),
    start: Optional[str] = Query(None, description="ISO-8601 start time (default: 24h ago)"),
    end: Optional[str] = Query(None, description="ISO-8601 end time (default: now)"),
    db: Session = Depends(get_db),
):
    """Query-based metrics endpoint for backward compatibility."""
    return get_metrics_internal(store_id, start, end, db)


@router.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
def get_metrics_path(
    store_id: str,
    start: Optional[str] = Query(None, description="ISO-8601 start time (default: 24h ago)"),
    end: Optional[str] = Query(None, description="ISO-8601 end time (default: now)"),
    db: Session = Depends(get_db),
):
    """Path-based metrics endpoint specified by the Part B rubric."""
    return get_metrics_internal(store_id, start, end, db)


def get_metrics_internal(store_id: str, start: Optional[str], end: Optional[str], db: Session) -> MetricsResponse:
    """Return business metrics for a store over a time window."""
    if end:
        end_dt = _parse_ts(end)
    else:
        last_ts = db.query(func.max(StoreEvent.timestamp)).filter(StoreEvent.store_id == store_id).scalar()
        if last_ts:
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            end_dt = last_ts
        else:
            end_dt = datetime.now(timezone.utc)

    if start:
        start_dt = _parse_ts(start)
    else:
        start_dt = end_dt - timedelta(hours=24)

    period = {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }

    # --- Entry / Exit counts (customer only, excluding staff) ---
    entry_count = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ENTRY",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    exit_count = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "EXIT",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    # --- Unique visitors (customer sessions) ---
    unique_visitors_q = db.query(
        func.count(func.distinct(VisitorSession.visitor_id))
    ).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
    )
    if start_dt:
        unique_visitors_q = unique_visitors_q.filter(
            VisitorSession.entry_time >= start_dt
        )
    if end_dt:
        unique_visitors_q = unique_visitors_q.filter(
            VisitorSession.entry_time <= end_dt
        )
    unique_visitors = unique_visitors_q.scalar() or 0

    # --- Average dwell (in seconds) ---
    sessions_with_dwell_q = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.dwell_seconds > 0,
    )
    if start_dt:
        sessions_with_dwell_q = sessions_with_dwell_q.filter(VisitorSession.entry_time >= start_dt)
    if end_dt:
        sessions_with_dwell_q = sessions_with_dwell_q.filter(VisitorSession.entry_time <= end_dt)
    sessions_with_dwell = sessions_with_dwell_q.all()

    if sessions_with_dwell:
        avg_dwell = sum(s.dwell_seconds for s in sessions_with_dwell) / len(sessions_with_dwell)
    else:
        avg_dwell = 0.0

    # --- Conversion rate ---
    billing_visitors_q = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.visited_billing == True,
    )
    if start_dt:
        billing_visitors_q = billing_visitors_q.filter(VisitorSession.entry_time >= start_dt)
    if end_dt:
        billing_visitors_q = billing_visitors_q.filter(VisitorSession.entry_time <= end_dt)
    billing_visitors = billing_visitors_q.all()

    billing_visitor_ids = {s.visitor_id for s in billing_visitors}
    converted_count = 0

    if billing_visitor_ids:
        # For each billing visitor, check if there's a POS transaction within
        # 30 minutes after their billing zone entry
        pos_txns = db.query(POSTransaction).filter(
            POSTransaction.store_id == store_id,
            POSTransaction.timestamp.between(start_dt, end_dt + timedelta(minutes=30)),
        ).all()

        pos_timestamps = sorted([t.timestamp for t in pos_txns])

        for visitor_id in billing_visitor_ids:
            # Get this visitor's billing zone entry time
            billing_entry_event = db.query(StoreEvent).filter(
                StoreEvent.store_id == store_id,
                StoreEvent.visitor_id == visitor_id,
                StoreEvent.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                StoreEvent.zone_id.isnot(None),
            ).order_by(StoreEvent.timestamp).first()

            if billing_entry_event is None:
                continue

            billing_entry_ts = billing_entry_event.timestamp
            if billing_entry_ts.tzinfo is None:
                billing_entry_ts = billing_entry_ts.replace(tzinfo=timezone.utc)

            # Check for POS transaction within 30-minute window
            window_end = billing_entry_ts + timedelta(minutes=30)
            for txn_ts in pos_timestamps:
                if txn_ts.tzinfo is None:
                    txn_ts = txn_ts.replace(tzinfo=timezone.utc)
                if billing_entry_ts <= txn_ts <= window_end:
                    converted_count += 1
                    break

    conversion_rate = round(converted_count / max(unique_visitors, 1), 4)

    # --- Re-entry rate ---
    reentry_sessions_q = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.reentry_count > 0,
    )
    if start_dt:
        reentry_sessions_q = reentry_sessions_q.filter(VisitorSession.entry_time >= start_dt)
    if end_dt:
        reentry_sessions_q = reentry_sessions_q.filter(VisitorSession.entry_time <= end_dt)
    reentry_sessions = reentry_sessions_q.count()
    reentry_rate = round(reentry_sessions / max(unique_visitors, 1), 4)

    # --- Staff movements ---
    staff_movements = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.is_staff == True,
        StoreEvent.event_type == "ENTRY",
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    # --- Currently inside ---
    currently_inside_q = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.entry_time.isnot(None),
        VisitorSession.exit_time.is_(None),
    )
    if end_dt:
        currently_inside_q = currently_inside_q.filter(VisitorSession.entry_time <= end_dt)
    currently_inside = currently_inside_q.count()

    # --- Peak hour ---
    peak_hour = _compute_peak_hour(db, store_id, start_dt, end_dt)

    # --- avg_dwell_per_zone (Real-time) ---
    exits = db.query(StoreEvent.zone_id, func.avg(StoreEvent.dwell_ms)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ZONE_EXIT",
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None),
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).group_by(StoreEvent.zone_id).all()

    avg_dwell_per_zone = {}
    for zone_id, avg_dwell_ms in exits:
        if avg_dwell_ms:
            avg_dwell_per_zone[zone_id] = round(avg_dwell_ms / 1000.0, 1)

    # Check fallbacks for zones with ZONE_DWELL but no ZONE_EXIT yet
    dwells = db.query(StoreEvent.zone_id, func.avg(StoreEvent.dwell_ms)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ZONE_DWELL",
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None),
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).group_by(StoreEvent.zone_id).all()
    for zone_id, avg_dwell_ms in dwells:
        if avg_dwell_ms and zone_id not in avg_dwell_per_zone:
            avg_dwell_per_zone[zone_id] = round(avg_dwell_ms / 1000.0, 1)

    # --- queue_depth (Real-time) ---
    latest_join = db.query(StoreEvent).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "BILLING_QUEUE_JOIN"
    ).order_by(StoreEvent.timestamp.desc()).first()

    queue_depth = 0
    if latest_join and latest_join.event_metadata:
        queue_depth = latest_join.event_metadata.get("queue_depth", 0) or 0

    # --- abandonment_rate (Real-time) ---
    abandons = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "BILLING_QUEUE_ABANDON",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).scalar() or 0

    joins = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "BILLING_QUEUE_JOIN",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).scalar() or 0

    abandonment_rate = round(abandons / max(joins, 1), 4)

    return MetricsResponse(
        store_id=store_id,
        period=period,
        total_entries=entry_count,
        total_exits=exit_count,
        unique_visitors=unique_visitors,
        avg_dwell_seconds=round(avg_dwell, 1),
        conversion_rate=conversion_rate,
        reentry_rate=reentry_rate,
        staff_movements=staff_movements,
        currently_inside=currently_inside,
        peak_hour=peak_hour,
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=queue_depth,
        abandonment_rate=abandonment_rate,
    )


def _compute_peak_hour(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> Optional[str]:
    """Find the hour with the most ENTRY events."""
    entries = db.query(StoreEvent).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ENTRY",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).all()

    if not entries:
        return None

    hour_counts: dict[str, int] = {}
    for e in entries:
        ts = e.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        hour_key = ts.strftime("%Y-%m-%dT%H:00:00Z")
        hour_counts[hour_key] = hour_counts.get(hour_key, 0) + 1

    return max(hour_counts, key=hour_counts.get) if hour_counts else None


def _parse_ts(ts_str: str) -> datetime:
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return datetime.now(timezone.utc)
