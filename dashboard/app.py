"""
dashboard/app.py — Streamlit live dashboard for Store Intelligence.

Run:  streamlit run dashboard/app.py
"""

import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

# ─────────────────────────────────────────────────────────────────
API_BASE = os.getenv("API_URL", "http://localhost:8000")
STORE_ID = os.getenv("STORE_ID", "STORE_BLR_002")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "5"))
EVENTS_FILE = os.getenv("EVENTS_FILE", "data/events.jsonl")
# ─────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Store Intelligence — Live Dashboard",
    page_icon="🏪",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  .metric-card {
    background: linear-gradient(135deg, #1e1e2e 0%, #2d2d44 100%);
    border: 1px solid #3d3d5c;
    border-radius: 12px;
    padding: 20px 24px;
    color: #e2e8f0;
    margin-bottom: 8px;
  }
  .metric-value { font-size: 2.2rem; font-weight: 700; color: #a78bfa; }
  .metric-label { font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }
  .metric-delta { font-size: 0.8rem; margin-top: 4px; }
  .delta-up { color: #34d399; }
  .delta-down { color: #f87171; }

  .anomaly-high   { background: #450a0a; border-left: 4px solid #ef4444; padding: 10px 14px; border-radius: 6px; margin: 6px 0; }
  .anomaly-medium { background: #431407; border-left: 4px solid #f97316; padding: 10px 14px; border-radius: 6px; margin: 6px 0; }
  .anomaly-low    { background: #1e3a5f; border-left: 4px solid #60a5fa; padding: 10px 14px; border-radius: 6px; margin: 6px 0; }

  div[data-testid="stMetric"] {
    background: linear-gradient(135deg, #1e1e2e, #2d2d44);
    border: 1px solid #3d3d5c;
    border-radius: 12px;
    padding: 16px 20px;
  }
  div[data-testid="stMetric"] label { color: #94a3b8 !important; }
  div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #a78bfa !important; font-weight: 700 !important; }

  .stSidebar { background: #0f0f1a; }
  .block-container { background: #0a0a14; }
  h1, h2, h3 { color: #e2e8f0; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict = None):
    try:
        r = requests.get(f"{API_BASE}{path}", params=params or {}, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def load_events_local(filepath: str, store_id: str) -> list[dict]:
    """Fallback: read events directly from JSONL when API is unreachable."""
    events = []
    if not os.path.exists(filepath):
        return events
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("store_id") == store_id:
                    events.append(e)
            except Exception:
                pass
    return events


# ─────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🏪 Store Intelligence")
    st.markdown("---")

    store_id = st.text_input("Store ID", value=STORE_ID)
    window_hours = st.slider("Time Window (hours)", 1, 48, 24)
    auto_refresh = st.toggle("Auto Refresh", value=True)
    refresh_interval = st.select_slider("Refresh Every", options=[5, 10, 30, 60], value=REFRESH_SECONDS)

    st.markdown("---")
    st.markdown("**API Status**")

    health = api_get("/health")
    if health and (health.get("status") in ("healthy", "warning") or health.get("database") == "online"):
        st.success("🟢 API Online")
    else:
        st.warning("🟡 API Offline — reading local file")

    if st.button("🔄 Refresh Now"):
        st.rerun()

    st.markdown("---")
    st.caption(f"Last update: {datetime.now().strftime('%H:%M:%S')}")


# ─────────────────────────────────────────────────────────────────
# FETCH DATA
# ─────────────────────────────────────────────────────────────────

end_dt = datetime.now(timezone.utc)
start_dt = end_dt - timedelta(hours=window_hours)

metrics = api_get("/metrics", {"store_id": store_id, "start": start_dt.isoformat(), "end": end_dt.isoformat()})
funnel_data = api_get("/funnel", {"store_id": store_id, "start": start_dt.isoformat(), "end": end_dt.isoformat()})
anomaly_data = api_get("/anomalies", {"store_id": store_id})
events_raw = api_get("/events", {"store_id": store_id, "limit": 500})


# ─────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────

st.markdown(f"""
<h1 style='color:#a78bfa; margin-bottom:4px;'>📊 Store Intelligence Dashboard</h1>
<p style='color:#64748b; font-size:0.9rem;'>
  {store_id} &nbsp;|&nbsp; Last {window_hours}h &nbsp;|&nbsp;
  {start_dt.strftime('%b %d %H:%M')} → {end_dt.strftime('%b %d %H:%M')} UTC
</p>
<hr style='border-color:#2d2d44; margin:8px 0 20px 0;'/>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# ROW 1: KPI METRICS
# ─────────────────────────────────────────────────────────────────

col1, col2, col3, col4, col5, col6 = st.columns(6)

if metrics:
    with col1:
        st.metric("👥 Unique Visitors", metrics.get("unique_visitors", "—"))
    with col2:
        st.metric("🚪 Entries", metrics.get("total_entries", "—"))
    with col3:
        st.metric("🚶 Exits", metrics.get("total_exits", "—"))
    with col4:
        cr = metrics.get("conversion_rate", 0)
        st.metric("💳 Conversion Rate", f"{cr:.1%}")
    with col5:
        dwell = metrics.get("avg_dwell_seconds", 0)
        st.metric("⏱ Avg Dwell", f"{int(dwell // 60)}m {int(dwell % 60)}s")
    with col6:
        inside = metrics.get("currently_inside", 0)
        st.metric("🏪 Inside Now", inside)
else:
    st.warning("⚠️ Could not load metrics from API. Is the API running?")


st.markdown("<hr style='border-color:#2d2d44; margin:16px 0;'/>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# ROW 2: FUNNEL + ANOMALIES
# ─────────────────────────────────────────────────────────────────

col_funnel, col_anomalies = st.columns([1.2, 0.8])

with col_funnel:
    st.markdown("### 🔽 Visitor Journey Funnel")
    if funnel_data and funnel_data.get("stages"):
        stages = funnel_data["stages"]
        labels = [s["stage"] for s in stages]
        values = [s["count"] for s in stages]
        rates = [s["rate"] for s in stages]

        fig_funnel = go.Figure(go.Funnel(
            y=labels,
            x=values,
            textinfo="value+percent initial",
            textfont={"color": "white", "size": 14},
            marker={
                "color": ["#7c3aed", "#6d28d9", "#5b21b6", "#4c1d95"],
                "line": {"width": 2, "color": "#a78bfa"},
            },
            connector={"line": {"color": "#3d3d5c", "width": 2}},
        ))
        fig_funnel.update_layout(
            paper_bgcolor="#0a0a14",
            plot_bgcolor="#0a0a14",
            font={"color": "#e2e8f0", "family": "Inter"},
            margin={"l": 20, "r": 20, "t": 10, "b": 10},
            height=280,
        )
        st.plotly_chart(fig_funnel, use_container_width=True)

        # Drop-off table
        drop = funnel_data.get("drop_off_analysis", {})
        drop_df = pd.DataFrame([
            {"Stage Transition": "Entry → Floor", "Drop-off": f"{drop.get('entry_to_floor', 0):.1%}"},
            {"Stage Transition": "Floor → Billing", "Drop-off": f"{drop.get('floor_to_billing', 0):.1%}"},
            {"Stage Transition": "Billing → Purchase", "Drop-off": f"{drop.get('billing_to_conversion', 0):.1%}"},
        ])
        st.dataframe(drop_df, hide_index=True, use_container_width=True)
    else:
        st.info("No funnel data available yet. Run the pipeline or generate demo data.")

with col_anomalies:
    st.markdown("### ⚠️ Anomaly Feed")
    if anomaly_data and anomaly_data.get("anomalies"):
        anomalies = anomaly_data["anomalies"]
        if anomalies:
            for a in anomalies[:6]:
                sev = a.get("severity", "LOW")
                css_class = f"anomaly-{sev.lower()}"
                icon = "🔴" if sev == "HIGH" else ("🟠" if sev == "MEDIUM" else "🔵")
                ts = a.get("timestamp", "")
                ts_display = f" @ {ts}" if ts else ""
                st.markdown(
                    f'<div class="{css_class}">'
                    f'<strong>{icon} {a["anomaly_type"]}</strong>{ts_display}<br/>'
                    f'<span style="font-size:0.85rem;color:#cbd5e1;">{a["description"]}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.success("✅ No anomalies detected")
    else:
        st.info("No anomaly data yet.")

    # Reentry / staff summary
    if metrics:
        st.markdown("---")
        rr = metrics.get("reentry_rate", 0)
        staff = metrics.get("staff_movements", 0)
        st.markdown(f"**🔁 Re-entry Rate:** `{rr:.1%}`")
        st.markdown(f"**👔 Staff Movements:** `{staff}`")
        peak = metrics.get("peak_hour")
        if peak:
            st.markdown(f"**⏰ Peak Hour:** `{peak}`")


# ─────────────────────────────────────────────────────────────────
# ROW 3: VISITOR TIMELINE + ZONE HEATMAP
# ─────────────────────────────────────────────────────────────────

st.markdown("---")
col_timeline, col_zones = st.columns([1.5, 1])

with col_timeline:
    st.markdown("### 📈 Visitor Timeline (Entries per 30 min)")

    events_list = []
    if events_raw and events_raw.get("events"):
        events_list = events_raw["events"]
    else:
        events_list = load_events_local(EVENTS_FILE, store_id)

    entry_events = [
        e for e in events_list
        if e.get("event_type") == "ENTRY" and not e.get("is_staff", False)
    ]

    if entry_events:
        df_entries = pd.DataFrame(entry_events)
        df_entries["timestamp"] = pd.to_datetime(df_entries["timestamp"])
        df_entries = df_entries.set_index("timestamp").sort_index()
        df_bucketed = df_entries.resample("30min").size().reset_index(name="entries")

        fig_timeline = px.area(
            df_bucketed,
            x="timestamp",
            y="entries",
            labels={"timestamp": "Time", "entries": "Entries"},
            color_discrete_sequence=["#7c3aed"],
        )
        fig_timeline.update_traces(
            fill="tozeroy",
            line={"color": "#a78bfa", "width": 2},
            fillcolor="rgba(124,58,237,0.2)",
        )
        fig_timeline.update_layout(
            paper_bgcolor="#0a0a14",
            plot_bgcolor="#12121f",
            font={"color": "#e2e8f0", "family": "Inter"},
            xaxis={"gridcolor": "#2d2d44", "color": "#94a3b8"},
            yaxis={"gridcolor": "#2d2d44", "color": "#94a3b8"},
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            height=250,
        )
        st.plotly_chart(fig_timeline, use_container_width=True)
    else:
        st.info("No entry events to display.")

with col_zones:
    st.markdown("### 🗺 Zone Dwell Heatmap")

    zone_events = [
        e for e in events_list
        if e.get("event_type") == "ZONE_DWELL" and e.get("zone_id") and not e.get("is_staff", False)
    ]

    if zone_events:
        zone_dwell: dict[str, int] = defaultdict(int)
        for e in zone_events:
            zone_dwell[e["zone_id"]] += e.get("dwell_ms", 0)

        zones_sorted = sorted(zone_dwell.items(), key=lambda x: -x[1])
        zone_labels = [z[0] for z in zones_sorted]
        zone_values = [z[1] / 60000 for z in zones_sorted]  # Convert to minutes

        fig_zones = go.Figure(go.Bar(
            x=zone_values,
            y=zone_labels,
            orientation="h",
            marker={
                "color": zone_values,
                "colorscale": "Purples",
                "showscale": False,
            },
            text=[f"{v:.1f}m" for v in zone_values],
            textposition="outside",
            textfont={"color": "#e2e8f0"},
        ))
        fig_zones.update_layout(
            paper_bgcolor="#0a0a14",
            plot_bgcolor="#12121f",
            font={"color": "#e2e8f0", "family": "Inter"},
            xaxis={"title": "Total Dwell (minutes)", "gridcolor": "#2d2d44", "color": "#94a3b8"},
            yaxis={"color": "#94a3b8"},
            margin={"l": 10, "r": 30, "t": 10, "b": 10},
            height=250,
        )
        st.plotly_chart(fig_zones, use_container_width=True)
    else:
        st.info("No zone dwell data yet.")


# ─────────────────────────────────────────────────────────────────
# ROW 4: RECENT EVENTS TABLE
# ─────────────────────────────────────────────────────────────────

st.markdown("---")
st.markdown("### 📋 Recent Events")

if events_list:
    recent = sorted(events_list, key=lambda e: e.get("timestamp", ""), reverse=True)[:20]
    df_recent = pd.DataFrame([{
        "Time": e.get("timestamp", "")[:19],
        "Visitor": e.get("visitor_id", ""),
        "Event": e.get("event_type", ""),
        "Zone": e.get("zone_id", "—") or "—",
        "Confidence": f"{e.get('confidence', 0):.2f}",
        "Staff": "👔" if e.get("is_staff") else "👤",
    } for e in recent])
    st.dataframe(df_recent, hide_index=True, use_container_width=True)
else:
    st.info("No events recorded yet. Run the detection pipeline to generate data.")


# ─────────────────────────────────────────────────────────────────
# AUTO-REFRESH
# ─────────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
