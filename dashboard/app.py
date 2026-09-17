"""
KrishiPulse — Agri-Mandi Price Anomaly Radar (Gujarat)

Improved dashboard:
1. Overview: headline KPIs and crop-wise summary
2. Price Trends: wholesale vs retail trends with crop/city/date filters
3. District Heat-Map: average retail premium (wedge %) by district
4. Anomaly Table: filtered anomalies with CSV download

Required environment:
- DATABASE_URL in Streamlit secrets or .env
- Tables: crop_master, mandi_master, wholesale_price, retail_price, price_anomaly
"""

import os
import re
import json
from datetime import date, datetime, timedelta

import pandas as pd
import psycopg2
import requests
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="KrishiPulse | Gujarat Price Radar",
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
# Helpers
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
    return psycopg2.connect(get_database_url())


@st.cache_data(ttl=600, show_spinner=False)
def run_query(query: str, params=None) -> pd.DataFrame:
    conn = None
    try:
        conn = get_connection()
        return pd.read_sql_query(query, conn, params=params)
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return pd.DataFrame()
    finally:
        if conn is not None:
            conn.close()


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
# Data access
# ---------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def get_date_bounds():
    result = run_query(
        "SELECT MIN(price_date) AS min_date, MAX(price_date) AS max_date "
        "FROM wholesale_price WHERE is_provisional = false"
    )

    today = date.today()
    if result.empty or pd.isna(result.loc[0, "min_date"]):
        return today - timedelta(days=1), today

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
        response = requests.get(GEOJSON_URL, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        st.warning(f"Could not load Gujarat map boundaries: {exc}")
        return None


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
def get_anomalies(crops, start_date, end_date):
    return run_query(
        """
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
            a.wedge_pct AS "Retail Premium %",
            a.anomaly_flag AS anomaly_flag
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE a.anomaly_flag = true
          AND c.crop_name IN %(crops)s
          AND a.price_date BETWEEN %(start)s AND %(end)s
        ORDER BY a.price_date DESC, ABS(a.wholesale_zscore) DESC NULLS LAST
        """,
        {"crops": crops, "start": start_date, "end": end_date},
    )


# ---------------------------------------------------------------------
# Sidebar
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
    "Wholesale: Agmarknet/data.gov.in. Retail: scraped and/or modeled "
    "records. Anomalies use a rolling baseline and z-score logic."
)

# ---------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------

st.title("🌾 KrishiPulse")
st.caption(
    "Agri-Mandi Price Anomaly Radar — Gujarat | "
    f"{start_date:%d %b %Y} to {end_date:%d %b %Y}"
)

tab_overview, tab_trends, tab_map, tab_anomalies = st.tabs(
    [
        "📊 Overview",
        "📈 Price Trends",
        "🗺️ District Heat-Map",
        "🚨 Anomaly Table",
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
    c4.metric("Days in Pipeline", f"{metrics['days']:,}")

    st.divider()
    st.subheader("Crop-wise Wholesale Summary")

    summary = get_crop_summary(query_crops, start_date, end_date)
    if summary.empty:
        st.info("No wholesale data for the selected filters.")
    else:
        display = summary.copy()
        for column in ["Min Price", "Max Price", "Avg Price"]:
            display[column] = display[column].apply(currency_fmt)
        st.dataframe(display, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------
# Tab 2: Trends
# ---------------------------------------------------------------------

with tab_trends:
    st.subheader("Wholesale vs Retail Price Trends")

    wholesale, retail = get_trend(
        query_crops, selected_city, start_date, end_date
    )

    if wholesale.empty and retail.empty:
        st.info("No price history is available for the selected filters.")
    else:
        fig = go.Figure()

        for crop in selected_crops:
            w = wholesale[wholesale["crop"] == crop]
            r = retail[retail["crop"] == crop]

            if not w.empty:
                fig.add_trace(
                    go.Scatter(
                        x=w["price_date"],
                        y=w["wholesale_price"],
                        mode="lines+markers",
                        name=f"{crop} — Wholesale",
                    )
                )

            if not r.empty:
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
            height=500,
            margin={"l": 0, "r": 0, "t": 20, "b": 0},
            legend_title_text="",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "Wholesale prices exclude provisional records. Retail prices are "
            "shown for the selected city or the average of available cities."
        )

# ---------------------------------------------------------------------
# Tab 3: District heat-map
# ---------------------------------------------------------------------

with tab_map:
    st.subheader("District Retail Premium (Wedge %)")

    wedge = get_district_wedge(query_crops, start_date, end_date)
    geojson = load_geojson()

    if geojson is None:
        st.warning("The Gujarat GeoJSON could not be loaded.")
    elif wedge.empty:
        st.info("No district wedge data is available for the selected filters.")
    else:
        wedge = wedge.copy()
        wedge["district_norm"] = wedge["district"].apply(normalize_district)
        map_df = (
            wedge.groupby("district_norm", as_index=False)
            .agg(
                avg_wedge_pct=("avg_wedge_pct", "mean"),
                records=("records", "sum"),
            )
        )

        fig = px.choropleth(
            map_df,
            geojson=geojson,
            locations="district_norm",
            featureidkey=f"properties.{GEOJSON_DISTRICT_KEY}",
            color="avg_wedge_pct",
            color_continuous_scale=["green", "yellow", "red"],
            labels={"avg_wedge_pct": "Average Wedge %"},
            hover_data={"records": True, "avg_wedge_pct": ":.2f"},
        )

        fig.update_geos(
            fitbounds="locations",
            visible=False,
            showcountries=False,
            showcoastlines=False,
            showland=False,
        )
        fig.update_layout(
            height=600,
            margin={"l": 0, "r": 0, "t": 0, "b": 0},
        )
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Top 5 Districts by Average Retail Premium")
        top5 = wedge.sort_values("avg_wedge_pct", ascending=False).head(5).copy()
        top5["avg_wedge_pct"] = top5["avg_wedge_pct"].apply(pct_fmt)
        top5 = top5.rename(
            columns={
                "district": "District",
                "avg_wedge_pct": "Average Wedge %",
                "records": "Records",
            }
        )
        st.dataframe(
            top5[["District", "Average Wedge %", "Records"]],
            use_container_width=True,
            hide_index=True,
        )

# ---------------------------------------------------------------------
# Tab 4: Anomalies
# ---------------------------------------------------------------------

with tab_anomalies:
    st.subheader("Flagged Wholesale/Retail Anomalies")

    anomalies = get_anomalies(query_crops, start_date, end_date)

    if anomalies.empty:
        st.success("No flagged anomalies for the selected filters.")
    else:
        display = anomalies.copy()
        display["Date"] = pd.to_datetime(display["Date"]).dt.strftime(
            "%Y-%m-%d"
        )
        display["Status"] = display["anomaly_flag"].map(
            lambda value: "🚨 Flagged" if value else ""
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

        st.download_button(
            "⬇️ Download Anomalies as CSV",
            data=anomalies.to_csv(index=False).encode("utf-8"),
            file_name=f"krishipulse_anomalies_{date.today()}.csv",
            mime="text/csv",
        )

st.markdown("---")
st.caption(
    "KrishiPulse | Gujarat district boundaries from "
    "udit-001/india-maps-data. Data interpretation depends on source "
    "coverage, provisional records, and baseline availability."
)