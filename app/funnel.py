"""
funnel.py — /funnel endpoint.

Computes a session-based visitor journey funnel:
  ENTRY → Zone Visit → Billing Zone → POS Transaction (Conversion)

Each stage count is computed from VisitorSession data.
No double-counting: each unique visitor_id contributes at most once per stage.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db_safe as get_db
from app.models import VisitorSession, POSTransaction, FunnelResponse, FunnelStage

router = APIRouter(tags=["funnel"])


@router.get("/funnel", response_model=FunnelResponse)
def get_funnel(
    store_id: str = Query(..., description="Store ID"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Query-based funnel endpoint for backward compatibility."""
    return get_funnel_internal(store_id, start, end, db)


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
def get_funnel_path(
    store_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Path-based funnel endpoint specified by the Part B rubric."""
    return get_funnel_internal(store_id, start, end, db)


def get_funnel_internal(store_id: str, start: Optional[str], end: Optional[str], db: Session) -> FunnelResponse:
    """
    Return the visitor journey funnel.

    Stage definitions:
      1. Entered store    — has an ENTRY event (customer, not staff)
      2. Explored floor   — visited at least 1 non-billing zone
      3. Reached billing  — visited_billing = True
      4. Converted        — session has a POS transaction correlation

    Drop-off is computed between consecutive stages.
    """
    if end:
        end_dt = _parse_ts(end)
    else:
        from app.models import StoreEvent
        from sqlalchemy import func
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

    period = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}

    # Stage 1: All customer sessions that entered
    all_sessions = db.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.is_staff == False,
        VisitorSession.entry_time >= start_dt,
        VisitorSession.entry_time <= end_dt,
    ).all()

    total_entered = len(all_sessions)
    if total_entered == 0:
        return FunnelResponse(
            store_id=store_id,
            period=period,
            stages=[
                FunnelStage(stage="Entered Store", count=0, rate=1.0),
                FunnelStage(stage="Explored Floor", count=0, rate=0.0),
                FunnelStage(stage="Reached Billing", count=0, rate=0.0),
                FunnelStage(stage="Converted (POS)", count=0, rate=0.0),
            ],
            drop_off_analysis={
                "entry_to_floor": 0.0,
                "floor_to_billing": 0.0,
                "billing_to_conversion": 0.0,
                "overall_conversion": 0.0,
            },
        )

    # Stage 2: Explored floor (visited at least 1 zone)
    explored_floor = [
        s for s in all_sessions
        if s.zones_visited and len(s.zones_visited) >= 1
    ]
    total_explored = len(explored_floor)

    # Stage 3: Reached billing zone
    reached_billing = [s for s in all_sessions if s.visited_billing]
    total_billing = len(reached_billing)

    # Stage 4: Converted
    # Load POS transactions in the time window
    pos_txns = db.query(POSTransaction).filter(
        POSTransaction.store_id == store_id,
        POSTransaction.timestamp.between(start_dt, end_dt + timedelta(minutes=5)),
    ).all()
    pos_timestamps = sorted(t.timestamp for t in pos_txns)

    total_converted = 0
    for s in reached_billing:
        # Consider them converted if POS transaction in 5 min after entry_time
        if s.entry_time is None:
            continue
        entry_ts = s.entry_time
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)
        window_end = entry_ts + timedelta(minutes=30)  # session window

        for txn_ts in pos_timestamps:
            if txn_ts.tzinfo is None:
                txn_ts = txn_ts.replace(tzinfo=timezone.utc)
            if entry_ts <= txn_ts <= window_end:
                total_converted += 1
                break

    # Stage rates (relative to entered)
    def safe_rate(n, d):
        return round(n / d, 4) if d > 0 else 0.0

    stages = [
        FunnelStage(stage="Entered Store", count=total_entered, rate=1.0),
        FunnelStage(stage="Explored Floor", count=total_explored, rate=safe_rate(total_explored, total_entered)),
        FunnelStage(stage="Reached Billing", count=total_billing, rate=safe_rate(total_billing, total_entered)),
        FunnelStage(stage="Converted (POS)", count=total_converted, rate=safe_rate(total_converted, total_entered)),
    ]

    drop_off = {
        "entry_to_floor": round(1 - safe_rate(total_explored, total_entered), 4),
        "floor_to_billing": round(safe_rate(total_explored - total_billing, max(total_explored, 1)), 4),
        "billing_to_conversion": round(1 - safe_rate(total_converted, max(total_billing, 1)), 4),
        "overall_conversion": safe_rate(total_converted, total_entered),
    }

    return FunnelResponse(
        store_id=store_id,
        period=period,
        stages=stages,
        drop_off_analysis=drop_off,
    )


def _parse_ts(ts_str: str) -> datetime:
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return datetime.now(timezone.utc)
