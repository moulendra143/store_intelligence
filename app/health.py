"""
health.py — /health endpoint.

Service status, last event timestamp per store, STALE_FEED warning if >10 min lag.
Must be accurate — this is what an on-call engineer checks first.
"""

from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db_safe as get_db
from app.models import StoreEvent

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    """
    Detailed service health check.
    Validates DB connectivity, returns last event timestamp per store,
    and returns a STALE_FEED warning if a store feed lag is > 10 minutes.
    """
    db_ok = False
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    if not db_ok:
        return {
            "status": "degraded",
            "database": "offline",
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "warnings": ["DATABASE_OFFLINE"]
        }

    # Query last event timestamp per store
    store_lags = db.query(
        StoreEvent.store_id,
        func.max(StoreEvent.timestamp)
    ).group_by(StoreEvent.store_id).all()

    now = datetime.now(timezone.utc)
    stores_status = {}
    warnings = []
    has_stale_feed = False

    for store_id, last_ts in store_lags:
        if last_ts is None:
            continue

        # Force tz-aware
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        lag_seconds = (now - last_ts).total_seconds()
        lag_minutes = lag_seconds / 60.0
        is_stale = (lag_minutes > 10.0)

        stores_status[store_id] = {
            "last_event_timestamp": last_ts.isoformat() + "Z",
            "lag_seconds": round(lag_seconds, 1),
            "lag_minutes": round(lag_minutes, 1),
            "status": "stale" if is_stale else "active"
        }

        if is_stale:
            has_stale_feed = True
            warnings.append(f"STALE_FEED: Store '{store_id}' lag is {lag_minutes:.1f} mins (> 10 mins).")

    # Overall system status
    status = "warning" if has_stale_feed else "healthy"

    return {
        "status": status,
        "database": "online",
        "timestamp": now.isoformat() + "Z",
        "stores": stores_status,
        "warnings": warnings,
        "version": "1.0.0"
    }
