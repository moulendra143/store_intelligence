"""
generate_demo_events.py — Synthetic data generator.

Generates realistic store intelligence events and POS transactions.
This allows the API to serve meaningful data immediately, even without
running the video pipeline.

Usage:
  python scripts/generate_demo_events.py
  python scripts/generate_demo_events.py --store STORE_BLR_002 --visitors 80
"""

import argparse
import csv
import json
import os
import random
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure we can write to data/
os.makedirs("data", exist_ok=True)


# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

STORES = [
    "STORE_BLR_002",
    "STORE_BLR_003",
    "STORE_MUM_001",
    "STORE_DEL_004",
    "STORE_CHN_001",
]

CAMERAS = {
    "entry": "CAM_ENTRY_01",
    "floor": "CAM_FLOOR_01",
    "billing": "CAM_BILLING_01",
}

ZONES = [
    "SKINCARE",
    "HAIRCARE",
    "FRAGRANCE",
    "MAKEUP",
    "SUPPLEMENTS",
    "BILLING",
]

BILLING_ZONES = {"BILLING"}

# Typical retail store open hours: 10:00 to 21:00
STORE_OPEN_HOUR = 10
STORE_CLOSE_HOUR = 21

# Visitor behaviour parameters
AVG_DWELL_SECONDS = 480     # 8 minutes average
STAFF_RATIO = 0.12          # 12% of "people" are staff
REENTRY_PROBABILITY = 0.08  # 8% of visitors re-enter
GROUP_ENTRY_PROBABILITY = 0.15  # 15% chance of group (2-3 together)
BILLING_VISIT_PROBABILITY = 0.45  # 45% reach billing
CONVERSION_PROBABILITY = 0.65    # 65% of billing visitors buy something
QUEUE_ABANDON_PROBABILITY = 0.20  # 20% abandon billing queue


def random_ts(base: datetime, max_offset_seconds: int) -> datetime:
    return base + timedelta(seconds=random.randint(0, max_offset_seconds))


def ts_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_event(
    store_id, camera_id, visitor_id, event_type,
    timestamp, zone_id=None, dwell_ms=0,
    is_staff=False, confidence=None, session_seq=0,
    queue_depth=None,
):
    if confidence is None:
        confidence = round(random.uniform(0.72, 0.96), 3)
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts_str(timestamp),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id,
            "session_seq": session_seq,
        },
    }


def generate_visitor_session(
    store_id: str,
    visitor_id: str,
    entry_time: datetime,
    is_staff: bool = False,
) -> tuple[list[dict], datetime | None]:
    """
    Generate a realistic visitor session as a list of events.
    Returns (events, pos_transaction_timestamp_or_None).
    """
    events = []
    seq = 0
    current_time = entry_time

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    # ENTRY
    events.append(make_event(
        store_id, CAMERAS["entry"], visitor_id, "ENTRY",
        current_time, is_staff=is_staff, session_seq=next_seq(),
    ))

    if is_staff:
        # Staff: quick entry, patrol a few zones, exit
        dwell = random.randint(60, 300)
        for zone in random.sample(ZONES[:4], k=random.randint(1, 3)):
            current_time += timedelta(seconds=random.randint(10, 60))
            events.append(make_event(
                store_id, CAMERAS["floor"], visitor_id, "ZONE_ENTER",
                current_time, zone_id=zone, is_staff=True, session_seq=next_seq(),
            ))
            current_time += timedelta(seconds=random.randint(30, 120))
            events.append(make_event(
                store_id, CAMERAS["floor"], visitor_id, "ZONE_EXIT",
                current_time, zone_id=zone, is_staff=True, session_seq=next_seq(),
                dwell_ms=random.randint(15000, 90000),
            ))
        current_time += timedelta(seconds=random.randint(10, 30))
        events.append(make_event(
            store_id, CAMERAS["entry"], visitor_id, "EXIT",
            current_time, is_staff=True, session_seq=next_seq(),
        ))
        return events, None

    # CUSTOMER: browse zones
    dwell_total = int(random.gauss(AVG_DWELL_SECONDS, AVG_DWELL_SECONDS * 0.4))
    dwell_total = max(60, min(dwell_total, 3600))

    zones_to_visit = random.sample(ZONES[:5], k=random.randint(1, 4))
    pos_transaction_ts = None

    for zone in zones_to_visit:
        current_time += timedelta(seconds=random.randint(15, 60))
        events.append(make_event(
            store_id, CAMERAS["floor"], visitor_id, "ZONE_ENTER",
            current_time, zone_id=zone, session_seq=next_seq(),
        ))

        zone_dwell = random.randint(30, max(30, dwell_total // max(len(zones_to_visit), 1)))

        # Emit periodic ZONE_DWELL events (every 30s)
        dwell_emitted = 0
        while dwell_emitted + 30 <= zone_dwell:
            current_time += timedelta(seconds=30)
            dwell_emitted += 30
            events.append(make_event(
                store_id, CAMERAS["floor"], visitor_id, "ZONE_DWELL",
                current_time, zone_id=zone, session_seq=next_seq(),
                dwell_ms=dwell_emitted * 1000,
            ))

        current_time += timedelta(seconds=max(0, zone_dwell - dwell_emitted))
        events.append(make_event(
            store_id, CAMERAS["floor"], visitor_id, "ZONE_EXIT",
            current_time, zone_id=zone, session_seq=next_seq(),
            dwell_ms=zone_dwell * 1000,
        ))

    # Billing zone visit
    visited_billing = random.random() < BILLING_VISIT_PROBABILITY
    if visited_billing:
        current_time += timedelta(seconds=random.randint(10, 30))
        queue_depth = random.randint(0, 4)

        event_type = "BILLING_QUEUE_JOIN" if queue_depth > 0 else "ZONE_ENTER"
        events.append(make_event(
            store_id, CAMERAS["billing"], visitor_id, event_type,
            current_time, zone_id="BILLING", session_seq=next_seq(),
            queue_depth=queue_depth if queue_depth > 0 else None,
        ))

        # Abandonment?
        if random.random() < QUEUE_ABANDON_PROBABILITY:
            current_time += timedelta(seconds=random.randint(20, 120))
            events.append(make_event(
                store_id, CAMERAS["billing"], visitor_id, "BILLING_QUEUE_ABANDON",
                current_time, zone_id="BILLING", session_seq=next_seq(),
                queue_depth=max(0, queue_depth - 1),
            ))
        else:
            # Converted: wait in queue then get a POS transaction
            wait_time = random.randint(60, 300)
            current_time += timedelta(seconds=wait_time)
            if random.random() < CONVERSION_PROBABILITY:
                pos_transaction_ts = current_time + timedelta(seconds=random.randint(10, 120))

            events.append(make_event(
                store_id, CAMERAS["billing"], visitor_id, "ZONE_EXIT",
                current_time, zone_id="BILLING", session_seq=next_seq(),
                dwell_ms=wait_time * 1000,
            ))

    # EXIT
    current_time += timedelta(seconds=random.randint(10, 60))
    events.append(make_event(
        store_id, CAMERAS["entry"], visitor_id, "EXIT",
        current_time, session_seq=next_seq(),
    ))

    return events, pos_transaction_ts


def generate_store_day(
    store_id: str,
    date: datetime,
    num_visitors: int,
) -> tuple[list[dict], list[dict]]:
    """Generate a full day of events for one store."""
    all_events: list[dict] = []
    pos_transactions: list[dict] = []

    visitor_counter = 0

    # Distribute visitor arrivals across open hours with realistic peaks
    open_seconds = (STORE_CLOSE_HOUR - STORE_OPEN_HOUR) * 3600
    store_open = date.replace(hour=STORE_OPEN_HOUR, minute=0, second=0, microsecond=0)

    # Generate entry times with lunch and evening peaks
    entry_times = []
    for _ in range(num_visitors):
        # 11 hours: 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20
        hour_weights = [
            1, 2, 3, 4, 5,      # 10-14: gradual build
            6, 8, 10, 10, 6, 2  # 15-20: peak 17-18
        ]
        hour_offset = random.choices(range(STORE_OPEN_HOUR, STORE_CLOSE_HOUR),
                                     weights=hour_weights)[0]
        minute_offset = random.randint(0, 59)
        entry_time = date.replace(hour=hour_offset, minute=minute_offset,
                                  second=random.randint(0, 59), microsecond=0)
        entry_time = entry_time.replace(tzinfo=timezone.utc)
        entry_times.append(entry_time)

    entry_times.sort()

    for entry_time in entry_times:
        visitor_counter += 1
        is_staff = random.random() < STAFF_RATIO
        visitor_id = f"VIS_{visitor_counter:06d}"

        events, pos_ts = generate_visitor_session(store_id, visitor_id, entry_time, is_staff)
        all_events.extend(events)

        if pos_ts:
            pos_transactions.append({
                "store_id": store_id,
                "transaction_id": f"TXN_{uuid.uuid4().hex[:8].upper()}",
                "timestamp": ts_str(pos_ts),
                "basket_value_inr": round(random.uniform(200, 5000), 2),
            })

        # Re-entry
        if not is_staff and random.random() < REENTRY_PROBABILITY:
            reentry_time = entry_time + timedelta(minutes=random.randint(5, 30))
            if reentry_time.hour < STORE_CLOSE_HOUR:
                seq = 0
                reentry_event = make_event(
                    store_id, CAMERAS["entry"], visitor_id, "REENTRY",
                    reentry_time, session_seq=1,
                )
                all_events.append(reentry_event)

        # Group entry (generate 1-2 companions entering at same time)
        if not is_staff and random.random() < GROUP_ENTRY_PROBABILITY:
            num_companions = random.randint(1, 2)
            for _ in range(num_companions):
                visitor_counter += 1
                companion_id = f"VIS_{visitor_counter:06d}"
                companion_entry = entry_time + timedelta(seconds=random.randint(0, 3))
                companion_events, companion_pos = generate_visitor_session(
                    store_id, companion_id, companion_entry, is_staff=False
                )
                all_events.extend(companion_events)
                if companion_pos:
                    pos_transactions.append({
                        "store_id": store_id,
                        "transaction_id": f"TXN_{uuid.uuid4().hex[:8].upper()}",
                        "timestamp": ts_str(companion_pos),
                        "basket_value_inr": round(random.uniform(200, 5000), 2),
                    })

        # Inject empty store periods (5-10 min gaps) occasionally
        if visitor_counter % 20 == 0:
            # Don't add extra visitors for a gap
            pass

    # Sort all events by timestamp
    all_events.sort(key=lambda e: e["timestamp"])

    return all_events, pos_transactions


def main():
    parser = argparse.ArgumentParser(description="Generate demo store intelligence data")
    parser.add_argument("--stores", nargs="+", default=STORES[:2], help="Store IDs to generate")
    parser.add_argument("--visitors", type=int, default=60, help="Avg visitors per store per day")
    parser.add_argument("--days", type=int, default=1, help="Number of days to generate")
    parser.add_argument("--output-events", default="data/events.jsonl")
    parser.add_argument("--output-pos", default="data/pos_transactions.csv")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"\n{'='*60}")
    print(f"SYNTHETIC DATA GENERATOR")
    print(f"  Stores  : {args.stores}")
    print(f"  Visitors: ~{args.visitors}/store/day")
    print(f"  Days    : {args.days}")
    print(f"{'='*60}\n")

    all_events: list[dict] = []
    all_pos: list[dict] = []

    base_date = datetime(2026, 3, 3, 0, 0, 0, tzinfo=timezone.utc)

    for store_id in args.stores:
        for day_offset in range(args.days):
            day = base_date + timedelta(days=day_offset)
            num_visitors = int(random.gauss(args.visitors, args.visitors * 0.2))
            num_visitors = max(10, num_visitors)

            print(f"  Generating {store_id} {day.date()} ({num_visitors} visitors)...")
            events, pos = generate_store_day(store_id, day, num_visitors)
            all_events.extend(events)
            all_pos.extend(pos)
            print(f"    -> {len(events)} events, {len(pos)} POS transactions")

    # Write events JSONL
    with open(args.output_events, "w", encoding="utf-8") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")

    # Write POS CSV
    with open(args.output_pos, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"])
        writer.writeheader()
        writer.writerows(all_pos)

    print(f"\n{'='*60}")
    print(f"DONE")
    print(f"  Events written  : {len(all_events):,} -> {args.output_events}")
    print(f"  POS written     : {len(all_pos):,} -> {args.output_pos}")
    print(f"{'='*60}\n")

    # Quick validation
    event_types = {}
    for e in all_events:
        t = e["event_type"]
        event_types[t] = event_types.get(t, 0) + 1

    print("Event type breakdown:")
    for et, count in sorted(event_types.items(), key=lambda x: -x[1]):
        print(f"  {et:30s} {count:5d}")


if __name__ == "__main__":
    main()
