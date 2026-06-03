"""
emit.py — Event creation and emission helpers.

Supports:
- Creating fully schema-compliant events
- Saving to JSONL file
- Optionally POSTing to the FastAPI ingestion endpoint
"""

import uuid
import json
import requests
from datetime import datetime, timezone
from typing import Optional


def create_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.0,
    timestamp: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Create a fully schema-compliant store intelligence event.

    All fields are populated — low-confidence events are emitted with their
    actual confidence value rather than being suppressed, allowing downstream
    consumers to filter based on their own thresholds.
    """
    if metadata is None:
        metadata = {}

    # Ensure required metadata fields are present
    meta = {
        "queue_depth": metadata.get("queue_depth", None),
        "sku_zone": metadata.get("sku_zone", zone_id),
        "session_seq": metadata.get("session_seq", 0),
    }
    # Merge any additional metadata keys
    for k, v in metadata.items():
        if k not in meta:
            meta[k] = v

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(float(confidence), 4),
        "metadata": meta,
    }


def save_event(event: dict, filename: str = "data/events.jsonl"):
    """Append event to JSONL file."""
    import os
    os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else ".", exist_ok=True)
    with open(filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def post_event(event: dict, api_url: str):
    """
    POST event to the FastAPI ingestion endpoint.
    Silently drops on network error (pipeline continues).
    """
    try:
        resp = requests.post(
            f"{api_url.rstrip('/')}/events",
            json=event,
            timeout=2.0,
        )
        if resp.status_code not in (200, 201):
            print(f"  [WARN] API returned {resp.status_code} for event {event.get('event_id')}")
    except Exception as e:
        print(f"  [WARN] Could not POST event to API: {e}")