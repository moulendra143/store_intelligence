"""
anomalies.py — /stores/{store_id}/anomalies endpoint.

Detects operational store anomalies with severity ("INFO" | "WARN" | "CRITICAL")
and specific "suggested_action" guidelines.
"""

import statistics
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db_safe as get_db
from app.models import StoreEvent, VisitorSession, POSTransaction, AnomaliesResponse, AnomalyItem

router = APIRouter(tags=["anomalies"])


@router.get("/anomalies", response_model=AnomaliesResponse)
def get_anomalies(
    store_id: str = Query(..., description="Store ID"),
    window_hours: int = Query(24, description="Lookback window in hours"),
    db: Session = Depends(get_db),
):
    """Query-based anomalies endpoint for backward compatibility."""
    return get_anomalies_internal(store_id, window_hours, db)


@router.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
def get_anomalies_path(
    store_id: str,
    window_hours: int = Query(24, description="Lookback window in hours"),
    db: Session = Depends(get_db),
):
    """Path-based anomalies endpoint specified by the Part B rubric."""
    return get_anomalies_internal(store_id, window_hours, db)


def get_anomalies_internal(store_id: str, window_hours: int, db: Session) -> AnomaliesResponse:
    """Detect anomalies in visitor behaviour and system health."""
    last_ts = db.query(func.max(StoreEvent.timestamp)).filter(StoreEvent.store_id == store_id).scalar()
    if last_ts:
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        end_dt = last_ts
    else:
        end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=window_hours)

    anomalies: list[AnomalyItem] = []

    # 1. Rubric-specific: Queue Spike (average last 15 min > 3.0)
    anomalies.extend(_detect_queue_spike(db, store_id, end_dt))

    # 2. Rubric-specific: Conversion Drop vs 7-day average
    anomalies.extend(_detect_conversion_drop(db, store_id, end_dt))

    # 3. Rubric-specific: Dead Zone (no visits in last 30 minutes)
    anomalies.extend(_detect_dead_zone(db, store_id, end_dt))

    # 4. Existing detectors (mapped to INFO/WARN/CRITICAL and enriched with suggested actions)
    anomalies.extend(_detect_entry_spike(db, store_id, start_dt, end_dt))
    anomalies.extend(_detect_queue_abandonment(db, store_id, start_dt, end_dt))
    anomalies.extend(_detect_orphaned_entries(db, store_id))
    anomalies.extend(_detect_long_dwell(db, store_id, start_dt, end_dt))
    anomalies.extend(_detect_empty_store(db, store_id, start_dt, end_dt))
    anomalies.extend(_detect_entry_exit_mismatch(db, store_id, start_dt, end_dt))

    # Remove duplicates if any overlap
    seen = set()
    unique_anomalies = []
    for item in anomalies:
        key = (item.anomaly_type, item.description)
        if key not in seen:
            seen.add(key)
            unique_anomalies.append(item)

    # Sort by severity
    severity_order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    unique_anomalies.sort(key=lambda a: severity_order.get(a.severity, 3))

    return AnomaliesResponse(
        store_id=store_id,
        generated_at=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        anomalies=unique_anomalies,
        total_count=len(unique_anomalies),
    )


# ------------------------------------------------------------------
# RUBRIC-SPECIFIC DETECTORS
# ------------------------------------------------------------------

def _detect_queue_spike(db: Session, store_id: str, end_dt: datetime) -> list[AnomalyItem]:
    """Detect high queue depth spikes in the last 15 minutes."""
    fifteen_mins_ago = end_dt - timedelta(minutes=15)

    recent_joins = db.query(StoreEvent).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "BILLING_QUEUE_JOIN",
        StoreEvent.timestamp.between(fifteen_mins_ago, end_dt)
    ).all()

    if not recent_joins:
        return []

    depths = []
    for r in recent_joins:
        if r.event_metadata:
            d = r.event_metadata.get("queue_depth", 0)
            if d is not None:
                depths.append(d)

    if not depths:
        return []

    avg_depth = sum(depths) / len(depths)
    THRESHOLD = 3.0

    if avg_depth > THRESHOLD:
        severity = "CRITICAL" if avg_depth > 5.0 else "WARN"
        return [AnomalyItem(
            anomaly_type="QUEUE_SPIKE",
            severity=severity,
            description=f"Billing queue is congested. Average queue depth in the last 15 minutes is {avg_depth:.1f} (Threshold: {THRESHOLD:.1f}).",
            timestamp=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            value=round(avg_depth, 2),
            threshold=THRESHOLD,
            suggested_action="Open additional billing registers immediately or deploy mobile checkout assistants to clear the queue."
        )]
    return []


def _detect_conversion_drop(db: Session, store_id: str, end_dt: datetime) -> list[AnomalyItem]:
    """Detect if the last 24h conversion rate drops by more than 30% vs the 7-day average."""
    # 1. 24h Conversion Rate (Today)
    today_start = end_dt - timedelta(hours=24)
    today_cr = _get_conversion_rate_in_window(db, store_id, today_start, end_dt)

    # 2. 7-day average daily conversion rate
    seven_days_start = end_dt - timedelta(days=7)
    seven_day_cr = _get_conversion_rate_in_window(db, store_id, seven_days_start, end_dt)

    # Check validation threshold
    if seven_day_cr <= 0.01:
        return []  # Avoid divide-by-zero or low data alarms

    # Prevent alarms on low visitor volume
    today_visitors = db.query(func.count(func.distinct(VisitorSession.visitor_id))).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.entry_time.between(today_start, end_dt)
    ).scalar() or 0

    if today_visitors < 5:
        return []

    drop_ratio = (seven_day_cr - today_cr) / seven_day_cr

    if drop_ratio >= 0.30:  # 30% drop
        severity = "CRITICAL" if drop_ratio >= 0.50 else "WARN"
        return [AnomalyItem(
            anomaly_type="CONVERSION_DROP",
            severity=severity,
            description=f"Conversion rate dropped significantly to {today_cr:.1%} vs 7-day average of {seven_day_cr:.1%}.",
            timestamp=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            value=round(today_cr, 4),
            threshold=round(seven_day_cr * 0.70, 4),
            suggested_action="Verify billing desk integration and verify if self-checkout or standard payment terminals are experiencing payment errors."
        )]
    return []


def _detect_dead_zone(db: Session, store_id: str, end_dt: datetime) -> list[AnomalyItem]:
    """Detect if an historically active store zone has received zero entries or dwells in the last 30 minutes."""
    thirty_mins_ago = end_dt - timedelta(minutes=30)

    # Find active zones by querying historical ENTER events
    historical_zones = db.query(
        StoreEvent.zone_id,
        func.count(StoreEvent.id)
    ).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type.in_(["ZONE_ENTER", "BILLING_QUEUE_JOIN"]),
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None)
    ).group_by(StoreEvent.zone_id).all()

    # Define historically active zones (at least 3 entries in total DB history)
    active_zones = [z for z, count in historical_zones if z and count >= 3]

    if not active_zones:
        return []

    # Get zones visited in the last 30 minutes
    recent_zone_events = db.query(StoreEvent.zone_id).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type.in_(["ZONE_ENTER", "ZONE_DWELL", "BILLING_QUEUE_JOIN"]),
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None),
        StoreEvent.timestamp.between(thirty_mins_ago, end_dt)
    ).distinct().all()

    recently_active_zones = {z[0] for z in recent_zone_events if z[0]}

    dead_zones = []
    for zone in active_zones:
        if zone not in recently_active_zones:
            severity = "WARN" if zone in ["MAKEUP", "SKINCARE", "BILLING"] else "INFO"
            dead_zones.append(AnomalyItem(
                anomaly_type="DEAD_ZONE",
                severity=severity,
                description=f"Zero customer activity recorded in zone '{zone}' for the last 30 minutes.",
                timestamp=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                suggested_action=f"Check for camera view obstruction or lens occlusion in the '{zone}' zone, or check if the shelves need restocking."
            ))
    return dead_zones


# ------------------------------------------------------------------
# COMPATIBLE & REFINED DETECTORS
# ------------------------------------------------------------------

def _detect_entry_spike(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> list[AnomalyItem]:
    """Detect entry spikes mapped to standard severities."""
    entries = db.query(StoreEvent).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ENTRY",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).all()

    if len(entries) < 2:
        return []

    hour_counts: dict[str, int] = {}
    for e in entries:
        ts = e.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        key = ts.strftime("%Y-%m-%dT%H:00:00Z")
        hour_counts[key] = hour_counts.get(key, 0) + 1

    if len(hour_counts) < 2:
        return []

    counts = list(hour_counts.values())
    mean = statistics.mean(counts)
    try:
        stdev = statistics.stdev(counts)
    except statistics.StatisticsError:
        return []

    threshold = mean + 2 * stdev
    result = []
    for hour, count in sorted(hour_counts.items()):
        if count > threshold:
            severity = "CRITICAL" if count > mean + 3 * stdev else "WARN"
            result.append(AnomalyItem(
                anomaly_type="ENTRY_SPIKE",
                severity=severity,
                description=f"Entry spike at {hour}: {count} entries (mean={mean:.1f}, threshold={threshold:.1f})",
                timestamp=hour,
                value=float(count),
                threshold=round(threshold, 1),
                suggested_action="Ensure entry channels are clear and optimize staff levels on checkout counters to meet higher buyer counts."
            ))
    return result


def _detect_queue_abandonment(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> list[AnomalyItem]:
    """Detect high billing queue abandonment rate."""
    abandons = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "BILLING_QUEUE_ABANDON",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    billing_joins = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    if billing_joins == 0:
        return []

    rate = abandons / billing_joins
    THRESHOLD = 0.40

    if rate > THRESHOLD:
        severity = "CRITICAL" if rate > 0.60 else "WARN"
        return [AnomalyItem(
            anomaly_type="HIGH_QUEUE_ABANDONMENT",
            severity=severity,
            description=f"Billing queue abandonment rate is {rate:.1%} ({abandons}/{billing_joins}).",
            value=round(rate, 4),
            threshold=THRESHOLD,
            suggested_action="Optimize checkout speeds or introduce secondary cashier lanes to prevent buyers from leaving without purchase."
        )]
    return []


def _detect_orphaned_entries(db: Session, store_id: str) -> list[AnomalyItem]:
    """Detect sessions that entered but never exited (potential tracking failure)."""
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

    orphaned = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.entry_time.isnot(None),
        VisitorSession.exit_time.is_(None),
        VisitorSession.entry_time <= two_hours_ago,
    ).count()

    if orphaned > 0:
        severity = "WARN" if orphaned >= 5 else "INFO"
        return [AnomalyItem(
            anomaly_type="ORPHANED_ENTRY",
            severity=severity,
            description=f"{orphaned} session(s) entered > 2 hours ago with zero exit. Potential tracking gaps.",
            value=float(orphaned),
            suggested_action="Inspect exit zone cameras for physical blocking or occlusion and check YOLO tracker settings."
        )]
    return []


def _detect_long_dwell(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> list[AnomalyItem]:
    """Detect unusually long dwell times (> 2.5× average)."""
    sessions = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.dwell_seconds > 0,
        VisitorSession.entry_time >= start_dt,
    ).all()

    if len(sessions) < 3:
        return []

    dwells = [s.dwell_seconds for s in sessions]
    mean_dwell = statistics.mean(dwells)
    threshold = mean_dwell * 2.5

    long_dwells = [s for s in sessions if s.dwell_seconds > threshold]
    if not long_dwells:
        return []

    return [AnomalyItem(
        anomaly_type="UNUSUALLY_LONG_DWELL",
        severity="INFO",
        description=f"{len(long_dwells)} visitors dwelled > 2.5× the store average dwell.",
        value=round(mean_dwell, 1),
        threshold=round(threshold, 1),
        suggested_action="Optimize display placement or verify if customers are confused or requiring direct staff support."
    )]


def _detect_empty_store(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> list[AnomalyItem]:
    """Detect periods of 10+ consecutive minutes with zero visitors."""
    events = db.query(StoreEvent).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type.in_(["ENTRY", "EXIT"]),
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).order_by(StoreEvent.timestamp).all()

    if not events:
        return [AnomalyItem(
            anomaly_type="EMPTY_STORE_PERIOD",
            severity="WARN",
            description="No visitor events recorded for the entire query window. Pipeline might be stopped.",
            value=float((end_dt - start_dt).total_seconds() / 60),
            threshold=10.0,
            suggested_action="Verify if the camera streams are working and double check if pipeline/run.py is running."
        )]

    occupancy = 0
    last_zero_ts = None
    empty_gaps = []

    for e in events:
        ts = e.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if e.event_type == "ENTRY":
            if occupancy == 0 and last_zero_ts is not None:
                gap_minutes = (ts - last_zero_ts).total_seconds() / 60
                if gap_minutes >= 10:
                    empty_gaps.append(gap_minutes)
            occupancy += 1
            last_zero_ts = None
        elif e.event_type == "EXIT":
            occupancy = max(0, occupancy - 1)
            if occupancy == 0:
                last_zero_ts = ts

    if empty_gaps:
        max_gap = max(empty_gaps)
        severity = "WARN" if max_gap > 30 else "INFO"
        return [AnomalyItem(
            anomaly_type="EMPTY_STORE_PERIOD",
            severity=severity,
            description=f"Empty store period detected: longest gap was {max_gap:.0f} minutes with zero visitors.",
            value=round(max_gap, 1),
            threshold=10.0,
            suggested_action="Check if camera inputs are dropping out or if the store was physically closed during normal hours."
        )]
    return []


def _detect_entry_exit_mismatch(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> list[AnomalyItem]:
    """Detect entry/exit mismatches above 20%."""
    entries = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ENTRY",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    exits = db.query(func.count(StoreEvent.id)).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "EXIT",
        StoreEvent.is_staff == False,
        StoreEvent.timestamp.between(start_dt, end_dt),
    ).scalar() or 0

    if entries == 0:
        return []

    mismatch_rate = abs(entries - exits) / entries
    THRESHOLD = 0.20

    if mismatch_rate > THRESHOLD:
        severity = "CRITICAL" if mismatch_rate > 0.40 else "WARN"
        return [AnomalyItem(
            anomaly_type="ENTRY_EXIT_MISMATCH",
            severity=severity,
            description=f"Entry/exit count mismatch of {mismatch_rate:.1%}: {entries} entries vs {exits} exits.",
            value=round(mismatch_rate, 4),
            threshold=THRESHOLD,
            suggested_action="Recalibrate spatial tracking gates and check if exits are occluded or if customers are exiting in crowds."
        )]
    return []


# ------------------------------------------------------------------
# INTERNAL CONVERSION WINDOW HELPER
# ------------------------------------------------------------------

def _get_conversion_rate_in_window(db: Session, store_id: str, start_dt: datetime, end_dt: datetime) -> float:
    """Helper to compute real-time conversion rates over a timeframe."""
    unique_visitors = db.query(func.count(func.distinct(VisitorSession.visitor_id))).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.entry_time.between(start_dt, end_dt)
    ).scalar() or 0

    if unique_visitors == 0:
        return 0.0

    billing_visitors = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.visited_billing == True,
        VisitorSession.entry_time.between(start_dt, end_dt)
    ).all()

    billing_visitor_ids = {s.visitor_id for s in billing_visitors}
    converted_count = 0

    if billing_visitor_ids:
        pos_txns = db.query(POSTransaction).filter(
            POSTransaction.store_id == store_id,
            POSTransaction.timestamp.between(start_dt, end_dt + timedelta(minutes=30)),
        ).all()
        pos_timestamps = sorted([t.timestamp for t in pos_txns])

        for visitor_id in billing_visitor_ids:
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

            window_end = billing_entry_ts + timedelta(minutes=30)
            for txn_ts in pos_timestamps:
                if txn_ts.tzinfo is None:
                    txn_ts = txn_ts.replace(tzinfo=timezone.utc)
                if billing_entry_ts <= txn_ts <= window_end:
                    converted_count += 1
                    break

    return round(converted_count / unique_visitors, 4)
