"""
data_adapter.py - Transform new datasets to match expected schemas.
"""
import csv
import json
import uuid
import os
from datetime import datetime

POS_IN = "data/POS - sample transactionsb1e826f.csv"
POS_OUT = "data/pos_transactions.csv"

EVENTS_IN = "data/sample_eventsbe42122.jsonl"
EVENTS_OUT = "data/events.jsonl"

def transform_pos():
    if not os.path.exists(POS_IN):
        print(f"Skipping POS transform, {POS_IN} not found.")
        return

    with open(POS_IN, 'r', encoding='utf-8') as f_in, open(POS_OUT, 'w', newline='', encoding='utf-8') as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"])
        writer.writeheader()
        
        orders = {}
        for row in reader:
            oid = row["order_id"]
            if oid not in orders:
                dt_str = f"{row['order_date']} {row['order_time']}"
                dt = datetime.strptime(dt_str, "%d-%m-%Y %H:%M:%S")
                orders[oid] = {
                    "store_id": row["store_id"],
                    "transaction_id": f"TXN_{oid}",
                    "timestamp": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "basket_value_inr": 0.0
                }
            orders[oid]["basket_value_inr"] += float(row["total_amount"])
            
        for oid, data in orders.items():
            writer.writerow(data)
    print(f"Generated {POS_OUT}")

def transform_events():
    if not os.path.exists(EVENTS_IN):
        print(f"Skipping events transform, {EVENTS_IN} not found.")
        return

    EVENT_TYPE_MAP = {
        "entry": "ENTRY",
        "exit": "EXIT",
        "zone_entered": "ZONE_ENTER",
        "zone_exited": "ZONE_EXIT",
        "queue_completed": "BILLING_QUEUE_JOIN",
        "queue_abandoned": "BILLING_QUEUE_ABANDON"
    }

    with open(EVENTS_IN, 'r', encoding='utf-8') as f_in, open(EVENTS_OUT, 'w', encoding='utf-8') as f_out:
        active_zones = {}  # key: (visitor_id, zone_id), value: enter_ts_str
        for line in f_in:
            line = line.strip()
            if not line: continue
            raw = json.loads(line)
            
            event_type = EVENT_TYPE_MAP.get(raw.get("event_type", ""), raw.get("event_type", "").upper())
            store_id = raw.get("store_code") or raw.get("store_id")
            if store_id:
                store_id = store_id.strip()
                if "1076" in store_id:
                    store_id = "ST1076"
                elif "1008" in store_id:
                    store_id = "ST1008"
            camera_id = raw.get("camera_id")
            visitor_id = str(raw.get("id_token") or raw.get("track_id"))
            
            TRACK_TO_ID = {
                "101": "ID_60001",
                "102": "ID_60002",
                "103": "ID_60003"
            }
            if visitor_id in TRACK_TO_ID:
                visitor_id = TRACK_TO_ID[visitor_id]
            
            ts_str = raw.get("event_timestamp") or raw.get("event_time") or raw.get("queue_join_ts")
            if ts_str and not ts_str.endswith("Z"):
                ts_str += "Z"
                
            zone_id = raw.get("zone_id")
            dwell_ms = 0
            
            if event_type == "ZONE_ENTER":
                active_zones[(visitor_id, zone_id)] = ts_str
            elif event_type == "ZONE_EXIT":
                enter_ts = active_zones.get((visitor_id, zone_id))
                if enter_ts:
                    try:
                        enter_dt = datetime.fromisoformat(enter_ts.replace("Z", "+00:00"))
                        exit_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        dwell_ms = int((exit_dt - enter_dt).total_seconds() * 1000)
                    except Exception:
                        pass
            elif "wait_seconds" in raw:
                dwell_ms = raw["wait_seconds"] * 1000
            
            is_staff = raw.get("is_staff", False)
            queue_depth = raw.get("queue_position_at_join")
            
            metadata = {
                "queue_depth": queue_depth,
                "sku_zone": raw.get("zone_name") or zone_id,
                "session_seq": 1
            }
            
            event = {
                "event_id": str(uuid.uuid4()),
                "store_id": store_id,
                "camera_id": camera_id,
                "visitor_id": visitor_id,
                "event_type": event_type,
                "timestamp": ts_str,
                "zone_id": zone_id,
                "dwell_ms": dwell_ms,
                "is_staff": is_staff,
                "confidence": 0.95,
                "metadata": metadata
            }
            
            f_out.write(json.dumps(event) + "\n")
    print(f"Generated {EVENTS_OUT}")

if __name__ == "__main__":
    # Ensure old DB is deleted so it starts fresh
    if os.path.exists("data/store_intelligence.db"):
        try:
            os.remove("data/store_intelligence.db")
            print("Deleted old data/store_intelligence.db")
        except Exception as e:
            print(f"Could not delete DB: {e}")
            
    transform_pos()
    transform_events()
    print("Data adapter completed.")
