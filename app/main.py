"""
main.py — FastAPI application entry point.

Wires all routers, startup events, middleware, and health checks.
Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import uuid
import json
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.database import init_db
from app.ingestion import router as ingestion_router, start_file_watcher, load_pos_transactions
from app.metrics import router as metrics_router
from app.funnel import router as funnel_router
from app.anomalies import router as anomalies_router
from app.heatmap import router as heatmap_router
from app.health import router as health_router


# ------------------------------------------------------------------
# STRUCTURED JSON LOGGER
# ------------------------------------------------------------------

class StructuredJSONFormatter(logging.Formatter):
    """Emits one JSON object per log line — machine-parseable by log aggregators."""
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge any extra fields added by request middleware
        for key in ("trace_id", "store_id", "endpoint", "method",
                    "latency_ms", "event_count", "status_code"):
            val = getattr(record, key, None)
            if val is not None:
                log_obj[key] = val
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def _setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_setup_logging()
logger = logging.getLogger("store_intelligence")

EVENTS_FILE = os.getenv("EVENTS_FILE", "data/events.jsonl")
POS_FILE = os.getenv("POS_FILE", "data/pos_transactions.csv")


# ------------------------------------------------------------------
# LIFESPAN — startup / shutdown
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, load POS data, start file-tail watcher."""
    logger.info("Starting Store Intelligence API...")

    # 1. Initialise database (creates tables)
    init_db()
    logger.info("Database initialised.")

    # 2. Bulk-load any existing events file
    if os.path.exists(EVENTS_FILE):
        logger.info("Loading existing events from %s...", EVENTS_FILE)
        from app.database import SessionLocal
        from app.ingestion import _rebuild_all_sessions
        import json as _json
        from sqlalchemy.exc import IntegrityError
        from app.models import StoreEvent
        from app.ingestion import _parse_timestamp

        db = SessionLocal()
        inserted = 0
        try:
            with open(EVENTS_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = _json.loads(line)
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
                    except Exception:
                        db.rollback()
            db.commit()
            logger.info("Loaded %d events from %s.", inserted, EVENTS_FILE)
            _rebuild_all_sessions(EVENTS_FILE)
            logger.info("Visitor sessions rebuilt.")
        finally:
            db.close()
    else:
        logger.info("No events file found at %s — starting fresh.", EVENTS_FILE)

    # 3. Load POS transactions
    if os.path.exists(POS_FILE):
        logger.info("Loading POS transactions from %s...", POS_FILE)
        from app.database import SessionLocal
        from app.ingestion import _parse_timestamp
        from app.models import POSTransaction
        from sqlalchemy.exc import IntegrityError
        import csv

        db = SessionLocal()
        inserted = 0
        try:
            with open(POS_FILE, newline="", encoding="utf-8") as f:
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
                    except Exception:
                        db.rollback()
            db.commit()
            logger.info("Loaded %d POS transactions.", inserted)
        finally:
            db.close()

    # 4. Start background file-tail watcher
    start_file_watcher(EVENTS_FILE)
    logger.info("File watcher started → watching %s", EVENTS_FILE)

    yield  # Application runs here

    logger.info("Shutting down Store Intelligence API.")
    from app.ingestion import stop_file_watcher
    stop_file_watcher()


# ------------------------------------------------------------------
# APP
# ------------------------------------------------------------------

app = FastAPI(
    title="Store Intelligence API",
    description="""
## Apex Retail — Store Intelligence API

Converts raw CCTV detection events into actionable business metrics.

### Key Endpoints
- **GET /metrics** — Store KPIs (entries, conversions, dwell time, etc.)
- **GET /funnel** — Visitor journey funnel with drop-off analysis
- **GET /anomalies** — Real-time anomaly detection
- **POST /events/ingest** — Ingest batches of detection events from the pipeline
- **GET /health** — Service health check

### Data Flow
```
CCTV → Detection Pipeline → POST /events/ingest → Database → GET /metrics
```
    """,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ------------------------------------------------------------------
# MIDDLEWARE
# ------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def structured_request_logger(request: Request, call_next):
    """
    Structured request logging middleware.

    Emits one JSON log line per request with:
      trace_id, store_id, endpoint, method, latency_ms, status_code
    event_count is injected by the ingest endpoint via request.state.
    """
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id

    # Extract store_id from path or query params
    store_id = (
        request.path_params.get("store_id")
        or request.query_params.get("store_id")
        or "-"
    )

    start = datetime.now(timezone.utc)

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        logger.error(
            "Unhandled exception",
            extra={
                "trace_id": trace_id,
                "store_id": store_id,
                "endpoint": request.url.path,
                "method": request.method,
                "latency_ms": duration_ms,
                "status_code": 500,
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "trace_id": trace_id,
            },
        )

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    event_count = getattr(request.state, "event_count", None)

    extra = {
        "trace_id": trace_id,
        "store_id": store_id,
        "endpoint": request.url.path,
        "method": request.method,
        "latency_ms": duration_ms,
        "status_code": response.status_code,
    }
    if event_count is not None:
        extra["event_count"] = event_count

    logger.info("request completed", extra=extra)
    return response


# ------------------------------------------------------------------
# ROUTERS
# ------------------------------------------------------------------

app.include_router(ingestion_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(anomalies_router)
app.include_router(heatmap_router)
app.include_router(health_router)


# ------------------------------------------------------------------
# CORE ENDPOINTS
# ------------------------------------------------------------------

@app.get("/", tags=["root"])
def root():
    return {
        "service": "Store Intelligence API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "endpoints": {
            "metrics": "/stores/STORE_BLR_002/metrics",
            "funnel": "/stores/STORE_BLR_002/funnel",
            "heatmap": "/stores/STORE_BLR_002/heatmap",
            "anomalies": "/stores/STORE_BLR_002/anomalies",
            "events_query": "/events?store_id=STORE_BLR_002",
            "events_ingest": "POST /events/ingest",
            "events_bulk": "POST /events/bulk",
            "pos_load": "POST /pos/load",
            "health": "/health",
        },
    }


# ------------------------------------------------------------------
# ERROR HANDLERS
# ------------------------------------------------------------------

@app.exception_handler(404)
async def not_found(request: Request, exc):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=404,
        content={
            "error": "Not found",
            "path": str(request.url.path),
            "trace_id": trace_id,
        },
    )


@app.exception_handler(500)
async def internal_error(request: Request, exc):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    logger.error(
        "Internal server error",
        extra={"trace_id": trace_id, "endpoint": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "trace_id": trace_id,
        },
    )