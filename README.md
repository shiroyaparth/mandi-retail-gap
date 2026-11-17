# 🌾 KrishiPulse - Agri-Mandi Price Anomaly Radar (Gujarat)

A data pipeline and dashboard that compares Gujarat's wholesale APMC mandi prices with retail prices and flags statistically unusual price movements, showing where retail prices sit far above what wholesale prices suggest.

**Live demo:** [mandi-retail-gap.streamlit.app](https://mandi-retail-gap.streamlit.app/)

---

## Problem Statement

Indian farmers typically receive only 30-40% of the final retail price of staple vegetables. The remaining 60-70% is absorbed by commission agents, transport intermediaries and regional distributors. Wholesale APMC mandi prices are publicly available, but consumers, kirana store owners and small food businesses have no easy way to compare them against what they are charged at the retail end.

KrishiPulse makes that gap visible.

---

## What It Does

- Ingests wholesale prices for 5 crops (Onion, Potato, Tomato, Garlic, Green Chilli) from 45 mandis in 23 Gujarat districts: from the data.gov.in API while it served Gujarat records, and from manual Agmarknet CSV imports since 26 Sep 2026
- Compares them with Blinkit Ahmedabad prices logged by hand since 18 Jul 2026, extended to five other Gujarat cities with a margin model
- Detects statistically unusual price moves with SQL rolling z-scores (30-day baseline, 2 standard deviations, 20% minimum move, 7-record minimum)
- Shows the results in a Streamlit dashboard with a Gujarat district heat-map, price trend charts and a flagged anomaly table
- Runs on a GitHub Actions schedule twice a day (9:00 AM and 2:30 PM IST): retail loading, margin model, anomaly detection and a data freshness check are automatic, and wholesale data arrives through a weekly CSV import

---

## Data Timeline

| Period | Wholesale source | Days with data |
|---|---|---|
| 20 Jun - 25 Sep 2026 | data.gov.in API, "Current Daily Price of Various Commodities from Various Markets (Mandi)" | 85 |
| 26 Sep - 9 Nov 2026 | Manual import of Agmarknet 2.0 Price and Arrivals Report CSV files | 45 |

The live API supplied about 65% of the wholesale days (85 of 130). After 25 Sep 2026 the feed stopped returning records for our Gujarat crops, and the cause has not been identified. Wholesale prices are therefore imported weekly from Agmarknet 2.0 reports. The API loader still runs on every scheduled run and writes through the same upsert as the CSV importer, so ingestion resumes through the same code path if the feed returns Gujarat records. This has not been exercised since the outage. Details and the evidence we kept are in `METHODOLOGY.md`.

---

## Tech Stack

| Component | Tool |
|---|---|
| Data ingestion and cleaning | Python (requests, psycopg2, pandas) |
| Wholesale fallback | Agmarknet 2.0 CSV import (pandas) |
| Database | PostgreSQL on Neon (free tier) |
| Anomaly detection | SQL window functions (rolling AVG and STDDEV) |
| Dashboard | Streamlit |
| Mapping | Plotly choropleth with a free Gujarat GeoJSON |
| Automation | GitHub Actions (twice-daily schedule) |
| Deployment | Streamlit Community Cloud |

---

## Project Structure

    mandi-retail-gap/
    ├── ingestion/
    │   ├── load_retail_manual.py        # Reads and validates retail prices from the Google Sheet
    │   └── generate_retail_model.py     # Builds modeled retail prices for five cities
    ├── pipeline/
    │   ├── load_master_data.py          # Seeds crop_master and crop_alias_map
    │   ├── load_wholesale_data.py       # Wholesale API loader (runs on every scheduled run)
    │   ├── import_agmarknet_file.py     # Weekly Agmarknet CSV importer
    │   ├── sources/                     # CSV parsing and the shared record format
    │   ├── anomaly_detection.sql        # Rolling z-score anomaly query
    │   ├── run_anomaly_detection.py     # Runs the anomaly SQL
    │   ├── check_freshness.py           # Fails the run if data is stale
    │   └── raw/                         # Saved raw API responses
    ├── dashboard/app.py                 # Streamlit dashboard (4 tabs)
    ├── db/schema.sql                    # Six-table PostgreSQL schema
    ├── migrations/002_multisource.sql   # Adds the source column and the ingestion_log table
    ├── data_inbox/                      # Drop zone for downloaded CSV files
    ├── docs/WEEKLY_IMPORT.md            # Weekly import checklist
    ├── .github/workflows/daily_ingestion.yml
    ├── .env.example
    ├── METHODOLOGY.md
    ├── requirements.txt
    └── README.md

---

## Data Sources

1. **data.gov.in API:** resource "Current Daily Price of Various Commodities from Various Markets (Mandi)" (ID 9ef84268-d588-465a-a308-a864a43d0070), Gujarat mandis, 5 crops, pulled twice a day. It supplied the data for 20 Jun - 25 Sep 2026.
2. **Agmarknet 2.0 CSV reports** (agmarknet.gov.in, Price and Arrivals Report): downloaded by hand once a week, they supply the data from 26 Sep 2026.
3. **Blinkit Ahmedabad:** retail prices logged by hand each day for all 5 crops since 18 Jul 2026, using the same product every day.
4. **Margin model:** Ahmedabad price multiplied by a city factor, Surat 1.05, Vadodara 1.03, Rajkot 1.08, Gandhinagar 1.02, Bhavnagar 1.10.
5. **Gujarat district GeoJSON:** open boundary data for the heat-map.

---

## Anomaly Detection

Each crop and mandi gets one price series, using the mandi's most-reported variety. For every day, a 30-day trailing average and standard deviation are computed with SQL window functions, excluding the current day. A wholesale price is flagged when its z-score is above 2 in absolute value and the price is at least 20% away from its baseline. At least 7 earlier records are required before a z-score is computed, and the latest 3 days of data are treated as provisional and excluded. Retail z-scores use only the logged Ahmedabad prices. Each mandi is compared with the retail price of its district's city: Ahmedabad uses logged Blinkit prices, five other cities use the modeled price, and all remaining districts use Ahmedabad as the Gujarat reference.

The `price_anomaly` table is rebuilt on every run. Current result: 608 flagged rows out of 7,548 scored rows (8.1%). See `METHODOLOGY.md` for the full method and its limits.

---

## Key Findings

- **Retail premium by crop.** Over the last 30 days of scored data (about 12 Oct - 6 Nov 2026), the average retail premium over the mandi price was highest for Green Chilli (about 424%), then Tomato (238%), Garlic (206%), Potato (141%) and Onion (92%). The premium is the average of daily mandi-level values of (retail price - wholesale price) / wholesale price. Blinkit is a quick-commerce channel, so these figures include convenience markups and are an upper-end indicator, not the intermediary margin alone.
- **Flags are spread across districts.** The six districts with the most flags (Rajkot 66, Navsari 64, Anand 48, Dahod 37, Bharuch 36, Kheda 36) each have a flag rate of 5-9% of their scored rows, so no single district stands out.
- **The detector finds large wholesale moves automatically.** Examples are Green Chilli in Gandhinagar on 25 Jul 2026 (z = +4.68) and Tomato in Rajkot on 23 Jul 2026 (z = -4.29, a fall). We did not establish causes. Some flags reflect a change in what a mandi reports rather than a real price move, for example Rajkot sub yard potato in October, which is described in `METHODOLOGY.md`.

---

## Operating the Pipeline

- The scheduled workflow runs the wholesale API loader (a failure there does not stop the run), the retail loader, the margin model, anomaly detection and the freshness check.
- Once a week, import the Agmarknet CSV files by following `docs/WEEKLY_IMPORT.md`.
- If the latest wholesale date is more than 8 days old, or the latest logged retail date is more than 4 days old, the freshness check fails the run.

---

## Local Setup

    # 1. Clone the repo
    git clone https://github.com/commit-dhruv/mandi-retail-gap.git
    cd mandi-retail-gap

    # 2. Create a virtual environment and install dependencies
    python -m venv .venv
    source .venv/bin/activate       # Linux/macOS (Fish shell: source .venv/bin/activate.fish)
    # .venv\Scripts\activate        # Windows
    pip install -r requirements.txt

    # 3. Set up environment variables
    cp .env.example .env
    # Edit .env: DATABASE_URL, RETAIL_SHEET_URL, DATA_GOV_API_KEY

    # 4. Create the schema, then apply the migration
    # Run db/schema.sql and then migrations/002_multisource.sql against your PostgreSQL database

    # 5. Seed master data
    python pipeline/load_master_data.py

    # 6. Load wholesale data from a downloaded CSV (see docs/WEEKLY_IMPORT.md)
    python pipeline/import_agmarknet_file.py

    # 7. Load retail data, build the model and run anomaly detection
    python ingestion/load_retail_manual.py
    python ingestion/generate_retail_model.py
    python pipeline/run_anomaly_detection.py

    # 8. Run the dashboard
    streamlit run dashboard/app.py

For Streamlit Cloud, add `DATABASE_URL` under Settings -> Secrets in TOML format. The dashboard only needs that one value.

---

## Design Decisions and Limitations

- **Retail data is logged by hand.** Blinkit uses JavaScript-rendered pages with authenticated requests, so automated scraping was ruled out after a feasibility test.
- **Estimated retail period.** Retail values from 20 Jun to 17 Jul 2026 are estimates, stored as modeled, not plotted on the dashboard, and still used as the retail reference for those dates.
- **Modeled cities.** Retail prices for five cities come from Ahmedabad with margin factors based on distance, not fitted to real data from those cities.
- **Wholesale refresh is weekly.** The API feed stopped returning Gujarat records for our crops after 25 Sep 2026, so wholesale data can be up to a week old plus the 3-day provisional window.
- **Missing days.** 13 calendar days between 20 Jun and 25 Sep have no wholesale records, and nothing was invented to fill them.
- **One variety per mandi.** Each mandi uses its most-reported variety, a stated simplification.
- **No machine learning.** Rolling z-scores are explainable, auditable and need no training data.
- **Flags are indicators.** A flagged row is a prompt to investigate, not proof of overcharging.

---

## Disclaimer

Retail prices for cities other than Ahmedabad are model estimates, and retail values before 18 July 2026 are estimates. Wholesale data since 26 September 2026 is imported weekly by hand. All methodology decisions are documented in `METHODOLOGY.md`.
