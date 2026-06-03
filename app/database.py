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
    """Initialize the database tables, ensuring the SQLite directory exists.

    For SQLite databases we need to make sure the directory for the database file
    exists. The ``DATABASE_URL`` environment variable can be either a relative path
    (e.g. ``sqlite:///./data/store_intelligence.db``) or an absolute path with
    four slashes (e.g. ``sqlite:////app/data/store_intelligence.db``). We parse the
    file component from the URL and create the parent directory if it does not
    already exist.
    """
    import os
    from urllib.parse import urlparse

    if DATABASE_URL.startswith("sqlite"):
        # Strip the ``sqlite:///`` prefix to get the filesystem path.
        # ``urlparse`` reliably handles the different numbers of slashes.
        parsed = urlparse(DATABASE_URL)
        # The path component may start with a leading slash for absolute paths.
        db_path = parsed.path
        # Remove leading '/' added by urlparse for relative paths like '///./data/...'
        if db_path.startswith("///"):
            db_path = db_path[2:]
        # Ensure we have a proper directory path.
        dir_path = os.path.dirname(db_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    from app.models import StoreEvent, POSTransaction, VisitorSession
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
