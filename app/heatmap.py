"""
heatmap.py — /stores/{store_id}/heatmap endpoint.

Zone visit frequency + avg dwell, normalised 0–100, ready for grid heatmap rendering.
Include data_confidence flag if fewer than 20 sessions in window.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db_safe as get_db
from app.models import StoreEvent, VisitorSession, HeatmapResponse, HeatmapItem

router = APIRouter(tags=["heatmap"])


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
def get_store_heatmap(
    store_id: str,
    start: Optional[str] = Query(None, description="ISO-8601 start time"),
    end: Optional[str] = Query(None, description="ISO-8601 end time"),
    db: Session = Depends(get_db),
):
    """
    Return zone visit frequency and average dwell times,
    normalised to a 0–100 scale, with a session density confidence check.
    """
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

    # 1. Calculate confidence flag (fewer than 20 sessions)
    total_sessions = db.query(func.count(func.distinct(VisitorSession.visitor_id))).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.entry_time.between(start_dt, end_dt)
    ).scalar() or 0
    data_confidence = (total_sessions >= 20)

    # 2. Get visit counts (frequency) per zone in window
    #    Only ZONE_ENTER counts — BILLING_QUEUE_JOIN feeds queue-depth metrics, not the spatial heatmap.
    visit_counts_raw = db.query(
        StoreEvent.zone_id,
        func.count(StoreEvent.id)
    ).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ZONE_ENTER",
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None),
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).group_by(StoreEvent.zone_id).all()

    visit_map = {zone_id: count for zone_id, count in visit_counts_raw if zone_id}

    # 3. Get average dwell per zone in window
    dwell_exits_raw = db.query(
        StoreEvent.zone_id,
        func.avg(StoreEvent.dwell_ms)
    ).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ZONE_EXIT",
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None),
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).group_by(StoreEvent.zone_id).all()

    dwell_map = {zone_id: round(avg_ms / 1000.0, 2) for zone_id, avg_ms in dwell_exits_raw if zone_id and avg_ms}

    # Check fallbacks for zones with ZONE_DWELL but no ZONE_EXIT yet
    dwell_fallback = db.query(
        StoreEvent.zone_id,
        func.avg(StoreEvent.dwell_ms)
    ).filter(
        StoreEvent.store_id == store_id,
        StoreEvent.event_type == "ZONE_DWELL",
        StoreEvent.is_staff == False,
        StoreEvent.zone_id.isnot(None),
        StoreEvent.timestamp.between(start_dt, end_dt)
    ).group_by(StoreEvent.zone_id).all()

    for zone_id, avg_ms in dwell_fallback:
        if zone_id and avg_ms and zone_id not in dwell_map:
            dwell_map[zone_id] = round(avg_ms / 1000.0, 2)

    # Compile the final list of zones present in the store's data
    all_zones = sorted(list(set(list(visit_map.keys()) + list(dwell_map.keys()))))
    
    if not all_zones:
        return HeatmapResponse(
            store_id=store_id,
            period=period,
            zones=[],
            data_confidence=data_confidence
        )

    # 4. Perform normalisation (0 - 100 scale)
    freq_values = [visit_map.get(z, 0) for z in all_zones]
    dwell_values = [dwell_map.get(z, 0.0) for z in all_zones]

    min_freq, max_freq = min(freq_values), max(freq_values)
    min_dwell, max_dwell = min(dwell_values), max(dwell_values)

    freq_range = max_freq - min_freq
    dwell_range = max_dwell - min_dwell

    zones_list = []
    for zone in all_zones:
        count = visit_map.get(zone, 0)
        avg_dwell = dwell_map.get(zone, 0.0)

        # Normalise Frequency
        if freq_range > 0:
            norm_freq = round(((count - min_freq) / freq_range) * 100.0, 1)
        else:
            norm_freq = 100.0 if max_freq > 0 else 0.0

        # Normalise Dwell
        if dwell_range > 0:
            norm_dwell = round(((avg_dwell - min_dwell) / dwell_range) * 100.0, 1)
        else:
            norm_dwell = 100.0 if max_dwell > 0.0 else 0.0

        zones_list.append(
            HeatmapItem(
                zone_id=zone,
                visit_count=count,
                avg_dwell_seconds=avg_dwell,
                normalized_frequency=norm_freq,
                normalized_dwell=norm_dwell
            )
        )

    return HeatmapResponse(
        store_id=store_id,
        period=period,
        zones=zones_list,
        data_confidence=data_confidence
    )


def _parse_ts(ts_str: str) -> datetime:
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return datetime.now(timezone.utc)
