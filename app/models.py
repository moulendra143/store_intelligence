"""
models.py — SQLAlchemy ORM models and Pydantic schemas.

Three tables:
  - store_events    : raw events from the detection pipeline
  - pos_transactions: POS records from pos_transactions.csv
  - visitor_sessions: aggregated per-visitor session summaries
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    DateTime, JSON, Index, UniqueConstraint
)
from sqlalchemy.orm import mapped_column, Mapped
from pydantic import BaseModel, Field

from app.database import Base


# ------------------------------------------------------------------
# ORM MODELS
# ------------------------------------------------------------------

class StoreEvent(Base):
    """Raw event from the detection pipeline."""
    __tablename__ = "store_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    visitor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    zone_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    dwell_ms: Mapped[int] = mapped_column(Integer, default=0)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    event_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_store_events_store_ts", "store_id", "timestamp"),
        Index("ix_store_events_visitor_type", "visitor_id", "event_type"),
    )


class POSTransaction(Base):
    """POS transaction from pos_transactions.csv."""
    __tablename__ = "pos_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    basket_value_inr: Mapped[float] = mapped_column(Float, default=0.0)


class VisitorSession(Base):
    """
    Aggregated session summary — one row per (store_id, visitor_id, entry_time).
    Populated by the ingestion service as events flow in.
    """
    __tablename__ = "visitor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    visitor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entry_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dwell_seconds: Mapped[int] = mapped_column(Integer, default=0)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    reentry_count: Mapped[int] = mapped_column(Integer, default=0)
    zones_visited: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    visited_billing: Mapped[bool] = mapped_column(Boolean, default=False)
    converted: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("store_id", "visitor_id", name="uq_session_store_visitor"),
        Index("ix_visitor_sessions_store", "store_id"),
    )


# ------------------------------------------------------------------
# PYDANTIC SCHEMAS (API request/response)
# ------------------------------------------------------------------

class EventMetadataSchema(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0

    class Config:
        extra = "allow"


class EventInSchema(BaseModel):
    """Schema for incoming events from the pipeline."""
    event_id: str = Field(..., description="UUID v4 — globally unique")
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str = Field(..., description="ENTRY | EXIT | ZONE_ENTER | ZONE_EXIT | ZONE_DWELL | BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON | REENTRY")
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp")
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: Optional[EventMetadataSchema] = None

    class Config:
        json_schema_extra = {
            "example": {
                "event_id": "550e8400-e29b-41d4-a716-446655440000",
                "store_id": "STORE_BLR_002",
                "camera_id": "CAM_ENTRY_01",
                "visitor_id": "VIS_000042",
                "event_type": "ENTRY",
                "timestamp": "2026-03-03T14:22:10Z",
                "zone_id": None,
                "dwell_ms": 0,
                "is_staff": False,
                "confidence": 0.91,
                "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
            }
        }


class EventOutSchema(EventInSchema):
    """Event as returned from the API."""
    id: int

    class Config:
        from_attributes = True


class MetricsResponse(BaseModel):
    store_id: str
    period: dict
    total_entries: int
    total_exits: int
    unique_visitors: int
    avg_dwell_seconds: float
    conversion_rate: float
    reentry_rate: float
    staff_movements: int
    currently_inside: int
    peak_hour: Optional[str] = None
    avg_dwell_per_zone: dict[str, float] = {}
    queue_depth: int = 0
    abandonment_rate: float = 0.0


class FunnelStage(BaseModel):
    stage: str
    count: int
    rate: float


class FunnelResponse(BaseModel):
    store_id: str
    period: dict
    stages: list[FunnelStage]
    drop_off_analysis: dict


class AnomalyItem(BaseModel):
    anomaly_type: str
    severity: str  # INFO | WARN | CRITICAL
    description: str
    timestamp: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    suggested_action: Optional[str] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    generated_at: str
    anomalies: list[AnomalyItem]
    total_count: int


# Heatmap Endpoints
class HeatmapItem(BaseModel):
    zone_id: str
    visit_count: int
    avg_dwell_seconds: float
    normalized_frequency: float
    normalized_dwell: float


class HeatmapResponse(BaseModel):
    store_id: str
    period: dict
    zones: list[HeatmapItem]
    data_confidence: bool


# Batch Event Ingest Endpoints
class IngestResultItem(BaseModel):
    event_id: Optional[str] = None
    status: str  # "ok", "duplicate", "error"
    error: Optional[str] = None


class IngestBatchResponse(BaseModel):
    status: str  # "success", "partial_success", "error"
    ingested_count: int
    skipped_count: int
    failed_count: int
    results: list[IngestResultItem]

