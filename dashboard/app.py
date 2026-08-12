import os
import json
from datetime import datetime

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="KrishiPulse - Gujarat Mandi-Retail Gap Radar",
    page_icon="🌾",
    layout="wide",
)

GEOJSON_URL = (
    "https://raw.githubusercontent.com/udit-001/india-maps-data/"
    "main/geojson/states/gujarat.geojson"
)

DISTRICT_NAME_FIX = {
    "Vadodara(Baroda)": "Vadodara",
    "Banaskanth": "Banaskantha",
    "Kachchh": "Kutch",
    "Junagarh": "Junagadh",
    "Mahesana": "Mehsana",
    "Panch Mahals": "Panchmahal",
    "Sabar Kantha": "Sabarkantha",
}

CROP_COLORS = {
    "Onion": "#B5651D",
    "Potato": "#D2B48C",
    "Tomato": "#C0392B",
    "Garlic": "#F5F5DC",
    "Green Chilli": "#2E7D32",
}


def get_database_url() -> str:
    try:
        return st.secrets["DATABASE_URL"]
    except (KeyError, FileNotFoundError):
        pass
    url = os.getenv("DATABASE_URL")
    if not url:
        st.error("DATABASE_URL not found. Set it in Streamlit Cloud Secrets, or in a local .env file.")
        st.stop()
    return url


def get_connection():
    return psycopg2.connect(get_database_url())


def run_query(sql, params=()):
    conn = get_connection()

    try:
        return pd.read_sql(sql, conn, params=params)

    except Exception:
        conn.rollback()
        raise


@st.cache_data(ttl=3600, show_spinner=False)
def load_gujarat_geojson():
    import urllib.request
    with urllib.request.urlopen(GEOJSON_URL) as resp:
        return json.load(resp)


@st.cache_data(ttl=600, show_spinner=False)
def load_crops():
    return run_query("SELECT crop_id, crop_name FROM crop_master ORDER BY crop_name")


@st.cache_data(ttl=600, show_spinner=False)
def load_cities():
    df = run_query("SELECT DISTINCT city FROM retail_price ORDER BY city")
    return df["city"].tolist()


@st.cache_data(ttl=600, show_spinner=False)
def load_wholesale_by_district(crop_id, include_provisional):
    clause = "" if include_provisional else "AND w.is_provisional = false"
    sql = f"""
        SELECT m.district,
               w.price_date,
               AVG(w.modal_price) AS avg_modal_price,
               COUNT(*) AS mandi_reports
        FROM wholesale_price w
        JOIN mandi_master m ON w.mandi_id = m.mandi_id
        WHERE w.crop_id = %s
          {clause}
          AND w.price_date = (
              SELECT MAX(price_date) FROM wholesale_price
              WHERE crop_id = %s {clause.replace('w.', '')}
          )
        GROUP BY m.district, w.price_date
    """
    df = run_query(sql, (crop_id, crop_id))
    if df.empty:
        return df
    df["district_norm"] = df["district"].replace(DISTRICT_NAME_FIX)
    return df


@st.cache_data(ttl=600, show_spinner=False)
def load_trend(crop_id, city):
    wholesale = run_query(
        """
        SELECT price_date, AVG(modal_price) AS wholesale_price
        FROM wholesale_price
        WHERE crop_id = %s AND is_provisional = false
        GROUP BY price_date
        ORDER BY price_date
        """,
        (crop_id,),
    )

    if city == "All cities (average)":
        retail = run_query(
            """
            SELECT price_date, AVG(price) AS retail_price
            FROM retail_price
            WHERE crop_id = %s
            GROUP BY price_date
            ORDER BY price_date
            """,
            (crop_id,),
        )
    else:
        retail = run_query(
            """
            SELECT price_date, AVG(price) AS retail_price
            FROM retail_price
            WHERE crop_id = %s AND city = %s
            GROUP BY price_date
            ORDER BY price_date
            """,
            (crop_id, city),
        )

    merged = pd.merge(wholesale, retail, on="price_date", how="outer").sort_values("price_date")
    return merged


@st.cache_data(ttl=600, show_spinner=False)
def load_anomalies(crop_name):
    clause = "" if crop_name == "All crops" else "AND c.crop_name = %s"
    params = () if crop_name == "All crops" else (crop_name,)
    sql = f"""
        SELECT
            c.crop_name AS "Crop",
            COALESCE(pa.district, '-') AS "District",
            COALESCE(pa.city, '-') AS "City",
            pa.price_date AS "Date",
            pa.wholesale_price AS "Wholesale Rs/kg",
            pa.wholesale_zscore AS "Wholesale Z",
            pa.retail_price AS "Retail Rs/kg",
            pa.retail_zscore AS "Retail Z",
            pa.wedge_pct AS "Wedge pct"
        FROM price_anomaly pa
        JOIN crop_master c ON pa.crop_id = c.crop_id
        WHERE pa.anomaly_flag = true
        {clause}
        ORDER BY pa.price_date DESC
    """
    return run_query(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def load_latest_kpis(crop_id, city):
    wholesale = run_query(
        """
        SELECT price_date, AVG(modal_price) AS price
        FROM wholesale_price
        WHERE crop_id = %s AND is_provisional = false
        GROUP BY price_date ORDER BY price_date DESC LIMIT 1
        """,
        (crop_id,),
    )
    if city == "All cities (average)":
        retail = run_query(
            """
            SELECT price_date, AVG(price) AS price
            FROM retail_price WHERE crop_id = %s
            GROUP BY price_date ORDER BY price_date DESC LIMIT 1
            """,
            (crop_id,),
        )
    else:
        retail = run_query(
            """
            SELECT price_date, AVG(price) AS price
            FROM retail_price WHERE crop_id = %s AND city = %s
            GROUP BY price_date ORDER BY price_date DESC LIMIT 1
            """,
            (crop_id, city),
        )

    w_price = float(wholesale["price"].iloc[0]) if not wholesale.empty else None
    r_price = float(retail["price"].iloc[0]) if not retail.empty else None
    wedge = None
    if w_price and r_price:
        wedge = round((r_price - w_price) / w_price * 100, 1)

    return {
        "wholesale_price": w_price,
        "wholesale_date": wholesale["price_date"].iloc[0] if not wholesale.empty else None,
        "retail_price": r_price,
        "retail_date": retail["price_date"].iloc[0] if not retail.empty else None,
        "wedge_pct": wedge,
    }


st.sidebar.title("KrishiPulse")
st.sidebar.caption("Gujarat wholesale vs retail price gap radar")

crops_df = load_crops()
if crops_df.empty:
    st.error("crop_master is empty - run pipeline/load_master_data.py first.")
    st.stop()

crop_name = st.sidebar.selectbox("Crop", crops_df["crop_name"].tolist())
crop_id = int(crops_df.loc[crops_df["crop_name"] == crop_name, "crop_id"].iloc[0])

cities = load_cities()
city_options = ["All cities (average)"] + cities
selected_city = st.sidebar.selectbox("Retail city (for trend/KPI)", city_options)

include_provisional = st.sidebar.checkbox(
    "Include today's provisional wholesale data on map",
    value=True,
    help="Provisional = reported in the last 3 days, not yet fully confirmed.",
)

st.sidebar.markdown("---")
st.sidebar.caption(
    "Data: Agmarknet (data.gov.in) wholesale prices, "
    "hybrid scraped + modeled retail prices. "
    "Anomalies = more than 2 std-dev deviation from 30-day rolling baseline."
)

st.title("KrishiPulse - Agri-Mandi Price Anomaly Radar")
st.caption("Where is the wholesale-to-retail markup in Gujarat's vegetable supply chain widest right now?")

kpis = load_latest_kpis(crop_id, selected_city)
k1, k2, k3 = st.columns(3)

with k1:
    if kpis["wholesale_price"] is not None:
        st.metric(
            "Latest wholesale (Gujarat avg)",
            f"Rs {kpis['wholesale_price']:.1f}/kg",
            help=f"As of {kpis['wholesale_date']}",
        )
    else:
        st.metric("Latest wholesale", "No data yet")

with k2:
    if kpis["retail_price"] is not None:
        st.metric(
            f"Latest retail ({selected_city})",
            f"Rs {kpis['retail_price']:.1f}/kg",
            help=f"As of {kpis['retail_date']}",
        )
    else:
        st.metric("Latest retail", "No data yet")

with k3:
    if kpis["wedge_pct"] is not None:
        st.metric("Retail markup over wholesale", f"{kpis['wedge_pct']:+.1f}%")
    else:
        st.metric("Retail markup over wholesale", "-")

st.markdown("---")

tab_map, tab_trend, tab_anomaly = st.tabs(
    ["District Heat-Map", "Trend Chart", "Anomaly Table"]
)

with tab_map:
    st.subheader(f"{crop_name} - wholesale modal price by district")

    map_df = load_wholesale_by_district(
        crop_id,
        include_provisional
    )

    geojson = load_gujarat_geojson()

    # Get ALL Gujarat districts from GeoJSON
    all_districts = pd.DataFrame(
        [
            {
                "district_norm": feature["properties"]["district"]
            }
            for feature in geojson["features"]
        ]
    ).drop_duplicates()

    # Add database data to all Gujarat districts
    full_map_df = all_districts.merge(
        map_df,
        on="district_norm",
        how="left"
    )

    if not map_df.empty:
        as_of = map_df["price_date"].iloc[0]
        total_reports = int(map_df["mandi_reports"].sum())

        st.caption(
            f"Snapshot as of {as_of} - "
            f"{total_reports} mandi reports"
        )
    else:
        st.info(
            "No wholesale price data is available for this crop. "
            "The complete Gujarat district map is still shown."
        )

    # Complete Gujarat map - white base
    fig = go.Figure()

    fig.add_trace(
        go.Choropleth(
            geojson=geojson,
            locations=full_map_df["district_norm"],
            z=[0] * len(full_map_df),
            featureidkey="properties.district",
            colorscale=[
                [0, "white"],
                [1, "white"]
            ],
            showscale=False,
            marker_line_color="#BDBDBD",
            marker_line_width=0.8,
            hoverinfo="skip",
        )
    )

    # Only districts having price data
    data_map_df = full_map_df[
        full_map_df["avg_modal_price"].notna()
    ].copy()

    if not data_map_df.empty:

        fig.add_trace(
            go.Choropleth(
                geojson=geojson,
                locations=data_map_df["district_norm"],
                z=data_map_df["avg_modal_price"],
                featureidkey="properties.district",
                colorscale="OrRd",
                colorbar=dict(
                    title="Rs/kg"
                ),
                marker_line_color="#777777",
                marker_line_width=0.8,
                customdata=data_map_df[
                    [
                        "district",
                        "avg_modal_price",
                        "mandi_reports"
                    ]
                ].values,
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "Avg wholesale: Rs %{customdata[1]:.1f}/kg<br>"
                    "Mandi reports: %{customdata[2]}"
                    "<extra></extra>"
                ),
            )
        )

    # Fit the map to the COMPLETE Gujarat map
    fig.update_geos(
        fitbounds="locations",
        visible=False,
        showcountries=False,
        showcoastlines=False,
        showland=False,
        bgcolor="white",
    )

    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        height=600,
        paper_bgcolor="white",
        plot_bgcolor="white",
        showlegend=False,
    )

    st.plotly_chart(
        fig,
        use_container_width=True
    )

    # Check districts which failed to match GeoJSON
    unmatched = set(map_df["district_norm"]) - set(
        feature["properties"]["district"]
        for feature in geojson["features"]
    )

    if unmatched:
        st.warning(
            "These districts didn't match the map boundaries: "
            + ", ".join(sorted(unmatched))
        )

    # Underlying district data
    with st.expander("Show underlying district data"):

        display_df = full_map_df[
            [
                "district_norm",
                "avg_modal_price",
                "mandi_reports"
            ]
        ].copy()

        display_df = display_df.rename(
            columns={
                "district_norm": "District",
                "avg_modal_price": "Avg Rs/kg",
                "mandi_reports": "Reports",
            }
        )

        display_df = display_df.sort_values(
            "Avg Rs/kg",
            ascending=False,
            na_position="last"
        )

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
        )

with tab_trend:
    st.subheader(f"{crop_name} - wholesale vs retail price over time")

    trend_df = load_trend(crop_id, selected_city)

    if trend_df.empty or trend_df[["wholesale_price", "retail_price"]].isna().all().all():
        st.info("Not enough price history yet to draw a trend line for this crop.")
    else:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=trend_df["price_date"],
                y=trend_df["wholesale_price"],
                name="Wholesale (Gujarat avg)",
                mode="lines+markers",
                line=dict(color="#1B5E20", width=2),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=trend_df["price_date"],
                y=trend_df["retail_price"],
                name=f"Retail ({selected_city})",
                mode="lines+markers",
                line=dict(color="#C0392B", width=2),
            )
        )
        fig.update_layout(
            xaxis_title="Date",
            yaxis_title="Rs per kg",
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            height=450,
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Wholesale line excludes provisional (last 3 days) records. "
            "Retail line mixes scraped and modeled rows - see the anomaly "
            "table's confidence notes for which is which."
        )

with tab_anomaly:
    st.subheader("Flagged wholesale/retail anomalies")
    st.caption("Rows where price deviates more than 2 standard deviations from its 30-day rolling baseline.")

    anomaly_crop_filter = st.radio(
        "Filter", ["This crop only", "All crops"], horizontal=True, key="anomaly_filter"
    )
    filter_name = crop_name if anomaly_crop_filter == "This crop only" else "All crops"

    anomaly_df = load_anomalies(filter_name)

    if anomaly_df.empty:
        st.success("No anomalies flagged yet for this selection - either things look normal, or there isn't enough price history (need 7+ days) for a baseline.")
    else:
        st.dataframe(
            anomaly_df.style.format(
                {
                    "Wholesale Rs/kg": "{:.1f}",
                    "Wholesale Z": "{:.2f}",
                    "Retail Rs/kg": "{:.1f}",
                    "Retail Z": "{:.2f}",
                    "Wedge pct": "{:+.1f}",
                },
                na_rep="-",
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "Download as CSV",
            anomaly_df.to_csv(index=False).encode("utf-8"),
            file_name=f"krishipulse_anomalies_{datetime.now().date()}.csv",
            mime="text/csv",
        )

st.markdown("---")
st.caption(
    "KrishiPulse - Gujarat district boundaries from udit-001/india-maps-data (community-curated, "
    "Census-derived) - Not for commercial/legal use."
)