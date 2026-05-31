"""
database.py — Database engine and session management.

Uses SQLite by default (zero-config, works without Docker).
Set DATABASE_URL env var to switch to PostgreSQL for production.

Examples:
  SQLite  (default): sqlite:///./data/store_intelligence.db
  PostgreSQL:        postgresql://user:pass@localhost:5432/store_intelligence
"""

import os
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import OperationalError as SAOperationalError

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./data/store_intelligence.db"
)

# SQLite needs check_same_thread=False for FastAPI's thread model
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

if DATABASE_URL in ("sqlite:///:memory:", "sqlite://"):
    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        poolclass=StaticPool,
        echo=False,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        echo=False,  # Set to True to log SQL queries
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a DB session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Called on API startup."""
    import os
    os.makedirs("data", exist_ok=True)
    # Import models to register them with Base
    from app.models import StoreEvent, POSTransaction, VisitorSession  # noqa: F401
    Base.metadata.create_all(bind=engine)


def get_db_safe():
    """
    FastAPI dependency: yields a DB session.
    Converts SQLAlchemy OperationalError → HTTP 503 with a structured body
    so no raw stack traces ever reach the client.
    """
    db = SessionLocal()
    try:
        yield db
    except SAOperationalError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable",
                "reason": "Database is temporarily unavailable. Please retry.",
            },
        ) from exc
    finally:
        db.close()
