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
    exists. ``DATABASE_URL`` can be a relative path (e.g. ``sqlite:///./data/store_intelligence.db``)
    or an absolute path with four slashes (e.g. ``sqlite:////app/data/store_intelligence.db``).
    This logic parses the path robustly and creates the parent directory if needed.
    """
    import os
    import time
    import logging
    from urllib.parse import urlparse

    logging.basicConfig(level=logging.INFO)

    if DATABASE_URL.startswith("sqlite"):
        # Parse the URL to extract the file path.
        parsed = urlparse(DATABASE_URL)
        # The path component may have leading slashes.
        raw_path = parsed.path
        # Determine if this is an absolute path (starts with //) or relative (single slash).
        if raw_path.startswith("//"):
            # Absolute path like "//app/data/store_intelligence.db"
            db_path = '/' + raw_path.lstrip('/')
        else:
            # Relative path like "/./data/store_intelligence.db" – remove leading slash and prepend the app root.
            rel_path = raw_path.lstrip('/')
            db_path = os.path.join("/app", rel_path)
        dir_path = os.path.dirname(db_path)
        if dir_path:
            logging.info(f"Ensuring SQLite directory exists: {dir_path}")
            os.makedirs(dir_path, exist_ok=True)

    # Import models after the directory is ready.
    from app.models import StoreEvent, POSTransaction, VisitorSession
    # Retry table creation – SQLite on a fresh volume can occasionally raise OperationalError.
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            logging.info("Database tables created successfully.")
            break
        except Exception as e:
            logging.warning(f"Database init attempt {attempt} failed: {e}")
            if attempt == max_retries:
                raise
            time.sleep(2)


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
