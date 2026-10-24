"""
KrishiPulse — Gujarat Mandi-Retail Price Gap & Anomaly Radar

Tabs:
1. 📊 Overview: Headline KPIs, stale data banner, crop-wise summary
2. 📈 Price Trends & Forecast: Wholesale vs retail trends + 7-day moving average forecast with honest uncertainty
3. 🌾 Farmer's Share Index: Mandi price as % of retail price over time and across cities
4. 🗺️ District Heat-Map: Retail premium (wedge %) by district with clear "no data" states
5. 🚨 Anomaly Table: Filtered anomalies with plain-English reasons, confidence levels, and CSV export
6. 🏥 Data Health & Audit: Per-source audit metrics, freshness checks, and ingestion logs

Data Sources:
- Wholesale: agmarknet.gov.in (Source acknowledged) & api.data.gov.in
- Retail: Scraped quick-commerce, modeled estimates, and official DCA (fcainfoweb.nic.in)
"""

import os
import re
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import psycopg2
import requests
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="KrishiPulse | Gujarat Price Gap Radar",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded",
)

CROPS = ["Onion", "Potato", "Tomato", "Garlic", "Green Chilli"]

GEOJSON_URL = (
    "https://raw.githubusercontent.com/udit-001/india-maps-data/"
    "main/geojson/states/gujarat.geojson"
)
GEOJSON_DISTRICT_KEY = "district"

DISTRICT_NAME_FIXES = {
    "Banaskanth": "Banaskantha",
    "Junagarh": "Junagadh",
    "Chhota Udepur": "Chhota Udaipur",
    "Devbhoomi Dwarka": "Devbhumi Dwarka",
    "Kachchh": "Kutch",
    "Vadodara(Baroda)": "Vadodara",
    "Mahesana": "Mehsana",
    "Panch Mahals": "Panchmahal",
    "Sabar Kantha": "Sabarkantha",
}


# ---------------------------------------------------------------------
# Database connection and Query Runner (no error caching!)
# ---------------------------------------------------------------------

def get_database_url() -> str:
    try:
        return st.secrets["DATABASE_URL"]
    except (KeyError, FileNotFoundError):
        value = os.getenv("DATABASE_URL")

    if not value:
        st.error(
            "DATABASE_URL was not found. Add it to Streamlit Secrets "
            "or your local .env file."
        )
        st.stop()

    return value


def get_connection():
    return psycopg2.connect(get_database_url(), connect_timeout=10)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_query(query: str, params_tuple=None) -> pd.DataFrame:
    """Internal cached query executor. Does NOT catch exceptions so errors are never cached."""
    conn = None
    try:
        conn = get_connection()
        params = dict(params_tuple) if params_tuple else None
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        df = pd.DataFrame(rows, columns=cols)
        cur.close()
        return df
    finally:
        if conn is not None:
            conn.close()


def run_query(query: str, params=None) -> pd.DataFrame:
    """Executes SQL query safely without caching failed / error results."""
    try:
        p_tuple = tuple(sorted(params.items())) if params else None
        return _cached_query(query, p_tuple)
    except Exception as exc:
        st.error(f"Database query error: {exc}")
        return pd.DataFrame()


def normalize_district(name):
    if pd.isna(name):
        return name
    name = re.sub(r"\s*\(.*?\)", "", str(name)).strip()
    return DISTRICT_NAME_FIXES.get(name, name)


def currency_fmt(value):
    return f"₹{float(value):,.2f}" if pd.notna(value) else "—"


def number_fmt(value):
    return f"{float(value):.2f}" if pd.notna(value) else "—"


def pct_fmt(value):
    return f"{float(value):+.2f}%" if pd.notna(value) else "—"


def empty_query_crops(crops):
    return tuple(crops) if crops else ("__NO_CROP_SELECTED__",)


# ---------------------------------------------------------------------
# Data access functions
# ---------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def get_date_bounds():
    result = run_query(
        "SELECT MIN(price_date) AS min_date, MAX(price_date) AS max_date "
        "FROM wholesale_price WHERE is_provisional = false"
    )

    today = date.today()
    if result.empty or pd.isna(result.loc[0, "min_date"]):
        return today - timedelta(days=30), today

    min_date = pd.to_datetime(result.loc[0, "min_date"]).date()
    max_date = (
        pd.to_datetime(result.loc[0, "max_date"]).date()
        if pd.notna(result.loc[0, "max_date"])
        else today
    )
    return min_date, max(today, max_date)


@st.cache_data(ttl=600, show_spinner=False)
def load_crops():
    return run_query(
        "SELECT crop_id, crop_name FROM crop_master ORDER BY crop_name"
    )


@st.cache_data(ttl=600, show_spinner=False)
def load_cities():
    result = run_query(
        "SELECT DISTINCT city FROM retail_price "
        "WHERE city IS NOT NULL ORDER BY city"
    )
    return result["city"].dropna().tolist() if not result.empty else []


@st.cache_data(ttl=600, show_spinner=False)
def load_geojson():
    try:
        response = requests.get(GEOJSON_URL, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        st.warning(f"Could not load Gujarat map boundaries: {exc}")
        return None


def prepare_geojson_districts(geojson):
    if not geojson:
        return geojson
    prepared = dict(geojson)
    prepared["features"] = []
    for feature in geojson.get("features", []):
        updated_feature = dict(feature)
        properties = dict(feature.get("properties", {}))
        original_name = properties.get(GEOJSON_DISTRICT_KEY)
        if original_name:
            properties[GEOJSON_DISTRICT_KEY] = normalize_district(original_name)
        updated_feature["properties"] = properties
        prepared["features"].append(updated_feature)
    return prepared


def get_all_geojson_districts(geojson):
    districts = []
    for feature in geojson.get("features", []):
        district = feature.get("properties", {}).get(GEOJSON_DISTRICT_KEY)
        if district:
            districts.append({"district_norm": normalize_district(district)})
    return pd.DataFrame(districts).drop_duplicates()


@st.cache_data(ttl=300, show_spinner=False)
def get_data_health():
    """Queries ingestion_log and table statistics for system health check."""
    last_wholesale = run_query("SELECT MAX(price_date) AS max_date FROM wholesale_price")
    last_retail = run_query("SELECT MAX(price_date) AS max_date FROM retail_price")

    wholesale_count = run_query("SELECT COUNT(*) AS n FROM wholesale_price")
    retail_count = run_query("SELECT COUNT(*) AS n FROM retail_price")
    anomaly_count = run_query("SELECT COUNT(*) AS n FROM price_anomaly")

    retail_by_type = run_query(
        "SELECT data_type, COUNT(*) AS count FROM retail_price GROUP BY data_type"
    )

    recent_logs = run_query(
        """
        SELECT source, status, records_fetched, records_inserted, error_message, started_at
        FROM ingestion_log
        ORDER BY started_at DESC
        LIMIT 10
        """
    )

    per_source_last_success = run_query(
        """
        SELECT source, MAX(started_at) AS last_success
        FROM ingestion_log
        WHERE status = 'success'
        GROUP BY source
        """
    )

    return {
        "last_wholesale_date": last_wholesale.loc[0, "max_date"] if not last_wholesale.empty else None,
        "last_retail_date": last_retail.loc[0, "max_date"] if not last_retail.empty else None,
        "wholesale_count": int(wholesale_count.loc[0, "n"]) if not wholesale_count.empty else 0,
        "retail_count": int(retail_count.loc[0, "n"]) if not retail_count.empty else 0,
        "anomaly_count": int(anomaly_count.loc[0, "n"]) if not anomaly_count.empty else 0,
        "retail_by_type": retail_by_type,
        "recent_logs": recent_logs,
        "per_source_last_success": per_source_last_success,
    }


@st.cache_data(ttl=600, show_spinner=False)
def get_overview_metrics(crops, start_date, end_date):
    records = run_query(
        """
        SELECT COUNT(*) AS n
        FROM wholesale_price w
        JOIN crop_master c ON c.crop_id = w.crop_id
        WHERE w.is_provisional = false
          AND c.crop_name IN %(crops)s
          AND w.price_date BETWEEN %(start)s AND %(end)s
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )

    anomalies = run_query(
        """
        SELECT COUNT(*) AS n
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE a.anomaly_flag = true
          AND c.crop_name IN %(crops)s
          AND a.price_date BETWEEN %(start)s AND %(end)s
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )

    districts = run_query(
        """
        SELECT COUNT(DISTINCT district) AS n
        FROM mandi_master
        WHERE district IS NOT NULL
        """
    )

    days = run_query(
        """
        SELECT (MAX(price_date) - MIN(price_date)) + 1 AS n
        FROM wholesale_price
        WHERE is_provisional = false
        """
    )

    def first_int(frame):
        if frame.empty or pd.isna(frame.loc[0, "n"]):
            return 0
        return int(frame.loc[0, "n"])

    return {
        "records": first_int(records),
        "anomalies": first_int(anomalies),
        "districts": first_int(districts),
        "days": first_int(days),
    }


@st.cache_data(ttl=600, show_spinner=False)
def get_crop_summary(crops, start_date, end_date):
    return run_query(
        """
        SELECT
            c.crop_name AS "Crop",
            COUNT(*) AS "Records",
            MIN(w.modal_price) AS "Min Price",
            MAX(w.modal_price) AS "Max Price",
            AVG(w.modal_price) AS "Avg Price"
        FROM wholesale_price w
        JOIN crop_master c ON c.crop_id = w.crop_id
        WHERE w.is_provisional = false
          AND c.crop_name IN %(crops)s
          AND w.price_date BETWEEN %(start)s AND %(end)s
        GROUP BY c.crop_name
        ORDER BY c.crop_name
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )


@st.cache_data(ttl=600, show_spinner=False)
def get_trend(crops, city, start_date, end_date):
    wholesale = run_query(
        """
        SELECT c.crop_name AS crop,
               w.price_date,
               AVG(w.modal_price) AS wholesale_price
        FROM wholesale_price w
        JOIN crop_master c ON c.crop_id = w.crop_id
        WHERE w.is_provisional = false
          AND c.crop_name IN %(crops)s
          AND w.price_date BETWEEN %(start)s AND %(end)s
        GROUP BY c.crop_name, w.price_date
        ORDER BY w.price_date
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )

    retail_city_clause = ""
    params = {"crops": crops, "start": start_date, "end": end_date}

    if city != "All cities (average)":
        retail_city_clause = "AND r.city = %(city)s"
        params["city"] = city

    retail = run_query(
        f"""
        SELECT c.crop_name AS crop,
               r.price_date,
               AVG(r.price) AS retail_price
        FROM retail_price r
        JOIN crop_master c ON c.crop_id = r.crop_id
        WHERE c.crop_name IN %(crops)s
          AND r.price_date BETWEEN %(start)s AND %(end)s
          {retail_city_clause}
        GROUP BY c.crop_name, r.price_date
        ORDER BY r.price_date
        """,
        params,
    )

    return wholesale, retail


@st.cache_data(ttl=600, show_spinner=False)
def get_farmers_share_data(crops, city, start_date, end_date):
    """Calculates Farmer's Share Index: (wholesale_price / retail_price) * 100."""
    city_filter = ""
    params = {"crops": crops, "start": start_date, "end": end_date}
    if city != "All cities (average)":
        city_filter = "AND a.city = %(city)s"
        params["city"] = city

    query = f"""
        SELECT
            c.crop_name AS crop,
            a.city,
            a.price_date,
            a.wholesale_price,
            a.retail_price,
            a.retail_data_type,
            ROUND((a.wholesale_price / NULLIF(a.retail_price, 0) * 100)::numeric, 2) AS farmers_share_pct
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE c.crop_name IN %(crops)s
          AND a.price_date BETWEEN %(start)s AND %(end)s
          AND a.retail_price > 0
          AND a.wholesale_price > 0
          {city_filter}
        ORDER BY a.price_date ASC
    """
    return run_query(query, params)


@st.cache_data(ttl=600, show_spinner=False)
def get_district_wedge(crops, start_date, end_date):
    return run_query(
        """
        SELECT
            a.district,
            AVG(a.wedge_pct) AS avg_wedge_pct,
            COUNT(*) AS records
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE a.district IS NOT NULL
          AND c.crop_name IN %(crops)s
          AND a.price_date BETWEEN %(start)s AND %(end)s
        GROUP BY a.district
        ORDER BY avg_wedge_pct DESC
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )


@st.cache_data(ttl=600, show_spinner=False)
def get_anomalies(crops, start_date, end_date, include_modeled=True):
    flag_filter = "WHERE a.anomaly_flag = true" if not include_modeled else "WHERE (a.anomaly_flag = true OR a.retail_data_type = 'modeled')"
    df = run_query(
        f"""
        SELECT
            c.crop_name AS "Crop",
            a.price_date AS "Date",
            COALESCE(a.district, '-') AS "District",
            COALESCE(a.city, '-') AS "City",
            a.wholesale_price AS "Wholesale Price",
            a.wholesale_baseline AS "30-Day Baseline",
            a.wholesale_zscore AS "Wholesale Z-Score",
            a.retail_price AS "Retail Price",
            a.retail_zscore AS "Retail Z-Score",
            a.wedge_pct AS "Retail Premium Pct",
            a.confidence_level AS "Confidence",
            a.retail_data_type AS "Retail Type",
            COALESCE(a.anomaly_reason, 'Normal') AS "Plain English Explanation",
            a.anomaly_flag AS anomaly_flag
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        {flag_filter}
          AND c.crop_name IN %(crops)s
          AND a.price_date BETWEEN %(start)s AND %(end)s
        ORDER BY a.price_date DESC, ABS(a.wholesale_zscore) DESC NULLS LAST
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )
    if not df.empty and "Retail Premium Pct" in df.columns:
        df = df.rename(columns={"Retail Premium Pct": "Retail Premium %"})
    return df



# ---------------------------------------------------------------------
# Sidebar Configuration
# ---------------------------------------------------------------------

st.sidebar.title("🌾 KrishiPulse")
st.sidebar.caption("Gujarat wholesale-to-retail price anomaly radar")

available_crops = load_crops()
if available_crops.empty:
    st.error("crop_master is empty. Run your master-data pipeline first.")
    st.stop()

crop_options = [
    crop for crop in CROPS if crop in available_crops["crop_name"].tolist()
]
if not crop_options:
    crop_options = available_crops["crop_name"].tolist()

selected_crops = st.sidebar.multiselect(
    "Crops",
    options=crop_options,
    default=crop_options,
)

cities = load_cities()
selected_city = st.sidebar.selectbox(
    "Retail city",
    ["All cities (average)"] + cities,
)

min_date, max_date = get_date_bounds()
default_start = max(min_date, max_date - timedelta(days=30))

selected_dates = st.sidebar.slider(
    "Date range",
    min_value=min_date,
    max_value=max_date,
    value=(default_start, max_date),
    format="DD MMM YYYY",
)

start_date, end_date = selected_dates
query_crops = empty_query_crops(selected_crops)

if not selected_crops:
    st.sidebar.warning("Select at least one crop to display data.")

st.sidebar.markdown("---")
st.sidebar.caption(
    "**Data Source Attribution:**\n"
    "Wholesale data source: [agmarknet.gov.in](https://agmarknet.gov.in) & api.data.gov.in.\n"
    "Retail: Quick-commerce monitoring & DCA [fcainfoweb.nic.in](https://fcainfoweb.nic.in)."
)


# ---------------------------------------------------------------------
# Top Header & Stale Data Check Banner
# ---------------------------------------------------------------------

health = get_data_health()
latest_wholesale_dt = health.get("last_wholesale_date")
is_stale = False
days_since_update = 0

if latest_wholesale_dt is not None:
    days_since_update = (date.today() - pd.to_datetime(latest_wholesale_dt).date()).days
    if days_since_update >= 2:
        is_stale = True

if is_stale:
    st.warning(
        f"⚠️ **Wholesale Mandi Data is Stale**: Last recorded wholesale arrival date was "
        f"**{latest_wholesale_dt}** ({days_since_update} days ago). Government APIs may be unreachable. "
        f"Place fresh Agmarknet downloads into `data_inbox/` to update."
    )

st.title("🌾 KrishiPulse")
st.caption(
    f"Gujarat Mandi-Retail Price Gap Radar | {start_date:%d %b %Y} to {end_date:%d %b %Y} | "
    "Data Source: **agmarknet.gov.in**"
)

tab_overview, tab_trends, tab_share, tab_map, tab_anomalies, tab_health = st.tabs(
    [
        "📊 Overview",
        "📈 Price Trends & Forecast",
        "🌾 Farmer's Share Index",
        "🗺️ District Heat-Map",
        "🚨 Anomaly Table",
        "🏥 Data Health & Audit",
    ]
)


# ---------------------------------------------------------------------
# Tab 1: Overview
# ---------------------------------------------------------------------

with tab_overview:
    metrics = get_overview_metrics(query_crops, start_date, end_date)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Wholesale Records", f"{metrics['records']:,}")
    c2.metric("Districts Covered", f"{metrics['districts']:,}")
    c3.metric("Anomalies Flagged", f"{metrics['anomalies']:,}")
    c4.metric("Days in Window", f"{metrics['days']:,}")

    st.divider()
    st.subheader("Crop-wise Wholesale Summary")

    summary = get_crop_summary(query_crops, start_date, end_date)
    if summary.empty:
        st.info("No wholesale data found for the selected filters.")
    else:
        display = summary.copy()
        for column in ["Min Price", "Max Price", "Avg Price"]:
            display[column] = display[column].apply(currency_fmt)
        st.dataframe(display, use_container_width=True, hide_index=True)
        st.caption("Wholesale prices in ₹/kg. Source: **agmarknet.gov.in** / data.gov.in.")


# ---------------------------------------------------------------------
# Tab 2: Price Trends & Simple Forecast
# ---------------------------------------------------------------------

with tab_trends:
    st.subheader("Wholesale vs Retail Price Trends & 7-Day Baseline Forecast")

    wholesale, retail = get_trend(
        query_crops, selected_city, start_date, end_date
    )

    show_forecast = st.checkbox("📈 Show 7-Day Moving Average Forecast & Uncertainty Band", value=True)

    if wholesale.empty and retail.empty:
        st.info("No price history is available for the selected filters.")
    else:
        fig = go.Figure()

        for crop in selected_crops:
            w = wholesale[wholesale["crop"] == crop].sort_values("price_date")
            r = retail[retail["crop"] == crop].sort_values("price_date")

            if not w.empty:
                w["price_date"] = pd.to_datetime(w["price_date"])
                fig.add_trace(
                    go.Scatter(
                        x=w["price_date"],
                        y=w["wholesale_price"],
                        mode="lines+markers",
                        name=f"{crop} — Mandi Wholesale",
                    )
                )

                if show_forecast and len(w) >= 7:
                    # 7-day rolling moving average
                    w["ma7"] = w["wholesale_price"].rolling(window=7, min_periods=3).mean()
                    std7 = w["wholesale_price"].rolling(window=7, min_periods=3).std().fillna(1.0)

                    # Simple seasonal-naive projection for next 7 days
                    last_date = w["price_date"].max()
                    last_ma = w["ma7"].iloc[-1]
                    last_std = std7.iloc[-1]

                    future_dates = [last_date + timedelta(days=i) for i in range(1, 8)]
                    future_vals = [last_ma] * 7
                    future_upper = [last_ma + 1.96 * last_std] * 7
                    future_lower = [max(1.0, last_ma - 1.96 * last_std)] * 7

                    # Forecast line
                    fig.add_trace(
                        go.Scatter(
                            x=future_dates,
                            y=future_vals,
                            mode="lines",
                            name=f"{crop} — 7d Forecast MA",
                            line={"dash": "dot", "width": 2},
                        )
                    )

                    # Uncertainty band
                    fig.add_trace(
                        go.Scatter(
                            x=future_dates + future_dates[::-1],
                            y=future_upper + future_lower[::-1],
                            fill="toself",
                            fillcolor="rgba(128, 128, 128, 0.15)",
                            line=dict(color="rgba(255,255,255,0)"),
                            name=f"{crop} — 95% Confidence Band",
                            showlegend=False,
                        )
                    )

            if not r.empty:
                r["price_date"] = pd.to_datetime(r["price_date"])
                fig.add_trace(
                    go.Scatter(
                        x=r["price_date"],
                        y=r["retail_price"],
                        mode="lines+markers",
                        name=f"{crop} — Retail",
                        line={"dash": "dash"},
                    )
                )

        fig.update_layout(
            xaxis_title="Date",
            yaxis_title="Price (₹/kg)",
            hovermode="x unified",
            height=520,
            margin={"l": 0, "r": 0, "t": 20, "b": 0},
            legend_title_text="",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.info(
            "💡 **Honest Forecast Uncertainty Note**: The projection uses a 7-day seasonal-naive moving average. "
            "Agricultural commodities are vulnerable to weather disruptions, transport strikes, and arrival shifts. "
            "Treat confidence bands as directional guides, not certainties."
        )
        st.caption("Wholesale source: **agmarknet.gov.in**. Retail source: quick-commerce & DCA.")


# ---------------------------------------------------------------------
# Tab 3: Farmer's Share Index
# ---------------------------------------------------------------------

with tab_share:
    st.subheader("🌾 Farmer's Share of the Consumer Rupee")
    st.caption(
        "Farmer's Share Index = (Wholesale Mandi Price ÷ Retail Price) × 100. "
        "Represents the percentage of consumer spend returned to the farm gate."
    )

    share_df = get_farmers_share_data(query_crops, selected_city, start_date, end_date)

    if share_df.empty:
        st.info("No paired wholesale-retail observations available in this date range.")
    else:
        share_df["price_date"] = pd.to_datetime(share_df["price_date"])

        # Summary KPIs
        avg_share = share_df["farmers_share_pct"].mean()
        min_share_row = share_df.loc[share_df["farmers_share_pct"].idxmin()]
        max_share_row = share_df.loc[share_df["farmers_share_pct"].idxmax()]

        col_s1, col_s2, col_s3 = st.columns(3)
        col_s1.metric("Average Farmer's Share", f"{avg_share:.1f}%")
        col_s2.metric(
            "Lowest Farmer's Share",
            f"{min_share_row['farmers_share_pct']:.1f}%",
            f"{min_share_row['crop']} ({min_share_row['city']})",
            delta_color="inverse",
        )
        col_s3.metric(
            "Highest Farmer's Share",
            f"{max_share_row['farmers_share_pct']:.1f}%",
            f"{max_share_row['crop']} ({max_share_row['city']})",
        )

        st.divider()

        # Farmer's share trend chart
        fig_share = px.line(
            share_df,
            x="price_date",
            y="farmers_share_pct",
            color="crop",
            line_dash="retail_data_type",
            labels={
                "price_date": "Date",
                "farmers_share_pct": "Farmer's Share (%)",
                "crop": "Crop",
                "retail_data_type": "Retail Source",
            },
            title="Farmer's Share Index Over Time",
        )
        # Add benchmark threshold line at 50% and 35%
        fig_share.add_hline(y=50, line_dash="dot", line_color="green", annotation_text="Healthy Farm Share (>50%)")
        fig_share.add_hline(y=35, line_dash="dot", line_color="red", annotation_text="High Wedge (<35%)")
        fig_share.update_layout(height=450, margin={"l": 0, "r": 0, "t": 40, "b": 0})
        st.plotly_chart(fig_share, use_container_width=True)

        # Crop-wise comparison bar chart
        crop_share = share_df.groupby("crop", as_index=False)["farmers_share_pct"].mean().sort_values("farmers_share_pct")
        fig_bar = px.bar(
            crop_share,
            x="crop",
            y="farmers_share_pct",
            color="farmers_share_pct",
            color_continuous_scale="Viridis",
            labels={"crop": "Crop", "farmers_share_pct": "Avg Farmer's Share (%)"},
            title="Average Farmer's Share by Crop",
        )
        fig_bar.update_layout(height=350, margin={"l": 0, "r": 0, "t": 40, "b": 0})
        st.plotly_chart(fig_bar, use_container_width=True)

        st.caption("Note: Dashed lines indicate modeled retail estimates. Solid lines indicate verified scraped/official retail.")


# ---------------------------------------------------------------------
# Tab 4: District Heat-Map
# ---------------------------------------------------------------------

with tab_map:
    st.subheader("District Retail Premium (Wedge %)")

    wedge = get_district_wedge(query_crops, start_date, end_date)
    geojson = load_geojson()

    if geojson is None:
        st.warning("The Gujarat GeoJSON map could not be loaded from network.")
    else:
        geojson = prepare_geojson_districts(geojson)
        all_districts = get_all_geojson_districts(geojson)

        if wedge.empty:
            map_df = all_districts.copy()
            map_df["avg_wedge_pct"] = None
            map_df["records"] = 0
            map_df["status"] = "No Data in Selected Window"
        else:
            wedge_clean = wedge.copy()
            wedge_clean["district_norm"] = wedge_clean["district"].apply(normalize_district)
            wedge_agg = (
                wedge_clean.groupby("district_norm", as_index=False)
                .agg(
                    avg_wedge_pct=("avg_wedge_pct", "mean"),
                    records=("records", "sum"),
                )
            )
            map_df = all_districts.merge(wedge_agg, on="district_norm", how="left")
            map_df["records"] = map_df["records"].fillna(0).astype(int)
            map_df["status"] = map_df["avg_wedge_pct"].apply(
                lambda v: f"{v:+.1f}%" if pd.notna(v) else "No Data"
            )

        # Plot choropleth map
        fig_map = px.choropleth(
            map_df,
            geojson=geojson,
            locations="district_norm",
            featureidkey=f"properties.{GEOJSON_DISTRICT_KEY}",
            color="avg_wedge_pct",
            color_continuous_scale=["green", "yellow", "red"],
            labels={"avg_wedge_pct": "Retail Wedge %"},
            hover_name="district_norm",
            hover_data={"records": True, "status": True, "avg_wedge_pct": ":.1f"},
        )
        fig_map.update_geos(
            fitbounds="locations",
            visible=False,
            showcountries=False,
            showcoastlines=False,
            showland=False,
        )
        fig_map.update_layout(height=580, margin={"l": 0, "r": 0, "t": 0, "b": 0})
        st.plotly_chart(fig_map, use_container_width=True)

        if not wedge.empty:
            st.subheader("Top Districts by Average Retail Markup")
            top5 = wedge.sort_values("avg_wedge_pct", ascending=False).head(5).copy()
            top5["avg_wedge_pct"] = top5["avg_wedge_pct"].apply(pct_fmt)
            top5 = top5.rename(
                columns={
                    "district": "District",
                    "avg_wedge_pct": "Average Wedge %",
                    "records": "Records",
                }
            )
            st.dataframe(top5[["District", "Average Wedge %", "Records"]], use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------
# Tab 5: Anomalies Table & Radar
# ---------------------------------------------------------------------

with tab_anomalies:
    st.subheader("🚨 Flagged Price Anomalies & Wedge Spikes")

    inc_modeled = st.checkbox("Show modeled estimates (for observation only, non-flagged)", value=False)
    anomalies = get_anomalies(query_crops, start_date, end_date, include_modeled=inc_modeled)

    if anomalies.empty:
        st.success("No flagged anomalies for the selected filters.")
    else:
        display = anomalies.copy()
        display["Date"] = pd.to_datetime(display["Date"]).dt.strftime("%Y-%m-%d")
        display["Status"] = display["anomaly_flag"].map(
            lambda v: "🚨 Flagged" if v else "ℹ️ Observed"
        )
        display = display.drop(columns=["anomaly_flag"])

        format_map = {
            "Wholesale Price": currency_fmt,
            "30-Day Baseline": currency_fmt,
            "Retail Price": currency_fmt,
            "Wholesale Z-Score": number_fmt,
            "Retail Z-Score": number_fmt,
            "Retail Premium %": pct_fmt,
        }

        st.dataframe(
            display.style.format(format_map, na_rep="—"),
            use_container_width=True,
            hide_index=True,
        )

        col_dl1, col_dl2 = st.columns(2)
        col_dl1.download_button(
            "⬇️ Download Anomalies (CSV)",
            data=anomalies.to_csv(index=False).encode("utf-8"),
            file_name=f"krishipulse_anomalies_{date.today()}.csv",
            mime="text/csv",
        )


# ---------------------------------------------------------------------
# Tab 6: Data Health & Pipeline Audit
# ---------------------------------------------------------------------

with tab_health:
    st.subheader("🏥 Ingestion Pipeline & Data Health")

    col_h1, col_h2, col_h3, col_h4 = st.columns(4)
    col_h1.metric("Wholesale Records", f"{health['wholesale_count']:,}")
    col_h2.metric("Retail Records", f"{health['retail_count']:,}")
    col_h3.metric("Anomalies Recorded", f"{health['anomaly_count']:,}")
    col_h4.metric(
        "Latest Mandi Date",
        str(health['last_wholesale_date']) if health['last_wholesale_date'] else "None",
        f"-{days_since_update}d" if latest_wholesale_dt else "No Data",
        delta_color="normal" if days_since_update < 2 else "inverse"
    )

    st.divider()

    st.subheader("Retail Records by Source Quality")
    if not health["retail_by_type"].empty:
        st.dataframe(health["retail_by_type"], use_container_width=True, hide_index=True)
    else:
        st.info("No retail records found in retail_price table.")

    st.subheader("Per-Source Last Successful Run")
    if not health["per_source_last_success"].empty:
        st.dataframe(health["per_source_last_success"], use_container_width=True, hide_index=True)
    else:
        st.info("No recorded successes yet in ingestion_log.")

    st.subheader("Recent Ingestion Run Logs (Audit Trail)")
    if not health["recent_logs"].empty:
        st.dataframe(health["recent_logs"], use_container_width=True, hide_index=True)
    else:
        st.info("ingestion_log table is currently empty. Run `pipeline/run_ingestion.py` to populate.")

    st.caption("Wholesale mandi prices courtesy of **agmarknet.gov.in** and api.data.gov.in.")


# ---------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------

st.markdown("---")
st.caption(
    "**KrishiPulse** | Wholesale price data source: **[agmarknet.gov.in](https://agmarknet.gov.in)** "
    "(Ministry of Agriculture & Farmers Welfare, Government of India) and api.data.gov.in. "
    "Retail price monitoring via quick-commerce platforms & Department of Consumer Affairs. "
    "All rights acknowledged."
)