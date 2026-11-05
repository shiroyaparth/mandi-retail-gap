# 🌾 KrishiPulse — Gujarat Mandi-Retail Price Gap & Anomaly Radar

> **Free, resilient, open-source agricultural price intelligence for Gujarat.**  
> Compares farm-gate APMC mandi wholesale arrivals with urban retail prices for key essential crops (**Onion, Potato, Tomato, Garlic, Green Chilli**) to uncover market anomalies, retail markups, and the Farmer's Share Index.

---

## 🏛️ Architecture & Data Flow

```mermaid
flowchart TD
    subgraph S_Wholesale ["Wholesale Mandi Data Sources"]
        A1["Agmarknet Manual Reports (CSV/Excel)"] -->|Drop into data_inbox/| A["csv_import Adapter (PRIMARY)"]
        A2["api.data.gov.in Resource"] -->|Automated API (India)| B["datagov_api Adapter (AUTO)"]
        A3["Agmarknet 2.0 API"] -.->|CAPTCHA-Gated: Documented Stub| C["agmarknet_api Adapter"]
    end

    subgraph S_Retail ["Retail Price Sources"]
        R1["Google Sheets (Quick-Commerce)"] -->|Scraped / Observed| R["load_retail_manual.py"]
        R2["fcainfoweb.nic.in (DCA Reports)"] -->|Official Gov Data| RO["load_retail_official.py"]
        R -->|Margin Multiplier| RM["generate_retail_model.py (Modeled)"]
    end

    A -->|Normalized Rows| Runner["pipeline/run_ingestion.py"]
    B -->|Normalized Rows| Runner

    Runner -->|Idempotent Upsert| DB[(PostgreSQL Database)]
    R -->|Idempotent Upsert| DB
    RO -->|Idempotent Upsert| DB
    RM -->|Idempotent Upsert| DB

    subgraph S_Engine ["Analytics & Anomaly Engine"]
        DB --> AnomEngine["pipeline/anomaly_detection.sql / run_anomaly_detection.py"]
        AnomEngine -->|Spatial City-District Join + 30d Rolling Window + MAD| DB
    end

    subgraph S_Outputs ["User Interfaces & Notification Services"]
        DB --> Dash["Streamlit Dashboard (app.py)"]
        DB --> Alerts["pipeline/alerts.py (Telegram / Email)"]
        DB --> Digest["pipeline/generate_weekly_digest.py (docs/digests/)"]
    end

    Runner -->|Audit Logs| Log[(ingestion_log)]
    Log --> Dash
```

---

## ✨ Key Features

1. **Multi-Source Wholesale Ingestion**:
   - **Primary**: Flexible CSV / Excel parser (`pipeline/import_agmarknet_file.py`) that auto-detects column synonyms, date formats (`DD/MM/YYYY`, `YYYY-MM-DD`, `DD-Mon-YYYY`), and price units (`Rs./Quintal` vs `Rs./Kg`).
   - **Automated Fallback**: Tries `data.gov.in` API when no local inbox files are present.
   - **Safe File Lifecycle**: Processed files are moved to `data_inbox/processed/` with collision prevention. Database history is never touched on failure.
2. **Quality-Aware Retail Hierarchy**:
   - Differentiates `official` (DCA / State Civil Supplies), `scraped` (quick-commerce platforms), and `modeled` retail prices.
   - Modeled estimates are clearly labelled and **never trigger false anomaly alarms**.
3. **Enhanced Spatial Anomaly Engine**:
   - **Spatial Precision**: Joins retail consumption centers to wholesale supply mandis using documented Gujarat `city -> district` mapping (Ahmedabad, Surat, Vadodara, Rajkot, Gandhinagar, Bhavnagar) — eliminating cross-district Cartesian mismatches.
   - **Time-Based 30-Day Windows**: Uses true calendar `RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING` instead of row counts.
   - **Confidence Scoring**: Small history samples (<5 days) are flagged with low confidence instead of silently vanishing.
   - **Robust Statistics**: Supports median and MAD (Median Absolute Deviation) scoring for outlier resilience.
4. **Interactive Dashboard (`app.py`)**:
   - **Overview**: Headline KPIs, crop summary tables, and stale data alerts.
   - **Price Trends & Forecast**: Wholesale vs retail trends with 7-day seasonal moving average forecasts and honest uncertainty intervals.
   - **Farmer's Share Index**: Visualizes what percentage of consumer retail spend reaches the farm gate ($\text{Mandi} \div \text{Retail} \times 100$).
   - **District Spread Heat-Map**: Full Gujarat choropleth with explicit "No Data" states and hover details.
   - **Anomaly Table**: Filterable table with plain English explanations and CSV export.
   - **Data Health Panel**: Live pipeline audit trail from `ingestion_log`.
   - **Bug Fix**: Transient database query errors are never poisoned into Streamlit's `@st.cache_data`.

---

## 🚀 Quickstart & Installation

### 1. Prerequisites
- Python 3.11+
- PostgreSQL (Local PostgreSQL 16+ or free cloud instance on [Neon](https://neon.tech) / [Supabase](https://supabase.com))

### 2. Setup Virtual Environment
```powershell
# Clone repo
git clone https://github.com/parthshiroya/mandi-retail-gap.git
cd mandi-retail-gap

# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # On Windows PowerShell
# source .venv/bin/activate    # On Linux / macOS

# Install dependencies
pip install -r requirements.txt
pip install pytest pytest-mock
```

### 3. Configure Environment
Copy `.env.example` to `.env` and fill in your connection details:
```powershell
Copy-Item .env.example .env
```
Edit `.env`:
```ini
DATABASE_URL=postgresql://user:password@localhost:5432/krishipulse
TEST_DATABASE_URL=postgresql://postgres@localhost:5433/kp_test
DATA_GOV_API_KEY=your_key_here
RETAIL_SHEET_URL=https://docs.google.com/spreadsheets/d/e/.../pub?gid=0&single=true&output=csv
```

### 4. Run Database Migrations
Apply migrations in numerical order against your database:
```powershell
# In PowerShell / psql:
python -c "
import psycopg2, os, dotenv
dotenv.load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
for f in ['db/schema.sql', 'migrations/001_constraints.sql', 'migrations/002_multisource.sql', 'migrations/003_retail_official.sql', 'migrations/004_anomaly_engine.sql']:
    cur.execute(open(f, encoding='utf-8').read())
    print('Applied:', f)
conn.commit()
conn.close()
"
```

### 5. Run Tests
Execute the full test suite against an isolated scratch test database:
```powershell
$env:TEST_DATABASE_URL="postgresql://postgres@localhost:5433/kp_test"
python -m pytest tests -q
# Result: 81 passed in ~8s
```

### 6. Launch the Dashboard
```powershell
streamlit run app.py
```
Open your browser to `http://localhost:8501`.

---

## 📋 Daily 5-Minute Manual Import Routine

When government APIs are down or geo-blocked on GitHub Actions, update KrishiPulse in 5 minutes:

1. **Download Agmarknet Report (2 minutes)**:
   - Visit [agmarknet.gov.in](https://agmarknet.gov.in) -> **Price and Arrival Report**.
   - Select: State = **Gujarat**, Commodities = **Onion, Potato, Tomato, Garlic, Green Chilli**, Date = **Today** (or last 3 days).
   - Click **Submit** -> Export / Download as **CSV** or **Excel (.xlsx)**.
2. **Drop File in Inbox (30 seconds)**:
   - Save or copy the downloaded file into `data_inbox/` (e.g. `data_inbox/Agmarknet_Report_20261024.csv`).
3. **Run Pipeline (1 minute)**:
   ```powershell
   python pipeline/run_ingestion.py
   python ingestion/load_retail_manual.py
   python ingestion/generate_retail_model.py
   python pipeline/run_anomaly_detection.py
   ```
4. **Done**:
   - The report is automatically parsed, validated, and upserted into PostgreSQL.
   - The file is safely archived into `data_inbox/processed/`.
   - Your Streamlit dashboard immediately reflects fresh data with updated Farmer's Share and anomaly flags!

---

## ⚙️ Automation Options (100% Free & Zero Laptop Dependency)

### Option A: 24/7 Zero-Laptop Cloud Automation (Oracle Cloud Always Free - Recommended ⭐)
Because Indian government portals (`api.data.gov.in` and `agmarknet.gov.in`) block foreign cloud IPs (Azure/AWS US/EU), run with an authentic Indian IP on Oracle Cloud Always Free in **Mumbai** or **Hyderabad**:
- **Cost**: **$0.00 / month forever**.
- **Setup Time**: 5 minutes using our automated script:
  ```bash
  git clone https://github.com/<your_username>/mandi-retail-gap.git ~/krishipulse
  cd ~/krishipulse && ./deploy/setup_oracle_free_tier.sh
  ```
- **How it works**: Registers your free VM as a GitHub Actions Self-Hosted Runner (`[self-hosted, linux, india]`). GitHub Actions schedules, manages, and logs daily runs at **09:00 AM IST**, while execution physically occurs in Mumbai without your laptop!
- See the full guide in [docs/cloud_automation_guide.md](docs/cloud_automation_guide.md).

### Option B: GitHub Actions Hosted (`ubuntu-latest` with India Proxy)
Included in `.github/workflows/daily_ingestion.yml`:
- **Schedule**: Twice daily at `03:30 UTC` (09:00 IST) and `09:00 UTC` (14:30 IST).
- **Proxy Support**: Set `INDIA_PROXY_URL` in GitHub Secrets to route requests through an Indian proxy.
- **Graceful Fallback & Instant Alerts**: If wholesale API connection is refused or throws HTTP 502, it sends an immediate Telegram / Email alert, preserves historical data, and completes retail and anomaly analysis.
- **Weekly Digest**: Runs every Sunday night via `.github/workflows/weekly_digest.yml` and commits `docs/digests/`.

### Option C: Windows Task Scheduler (Workstation in India)
If using your local Windows PC, run with `-StartWhenAvailable` so tasks run immediately whenever your laptop opens:
```powershell
# Schedule morning ingestion at 09:30 AM IST (runs on wake/unlock if laptop was closed)
schtasks /create /tn "KrishiPulse_Daily_Ingest" `
    /tr "powershell.exe -ExecutionPolicy Bypass -File \"D:\project\krishipulse\mandi-retail-gap\tools\run_daily_windows.ps1\"" `
    /sc daily /st 09:30 /f
```
See [`docs/windows_scheduler.md`](docs/windows_scheduler.md) for full instructions.

---

## ☁️ Free Cloud Hosting Guide

You can host both the database and dashboard for free:

### 1. Database: Neon Serverless PostgreSQL (Free Tier)
1. Sign up free at [neon.tech](https://neon.tech).
2. Create a project named `krishipulse` in region `ap-southeast-1` (Singapore) or `eu-central-1`.
3. Copy the pooled connection string (`postgresql://...@ep-pooler.tech/neondb?sslmode=require`).
4. Execute the schema and migration scripts against the Neon URL.

### 2. Dashboard: Streamlit Community Cloud (Free Tier)
1. Fork or push your repository to GitHub.
2. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub.
3. Click **New app** -> Select repository `mandi-retail-gap` -> Main branch -> Path: `app.py`.
4. Click **Advanced Settings** -> **Secrets**:
   ```toml
   DATABASE_URL = "postgresql://user:password@ep-pooler.tech/neondb?sslmode=require"
   ```
5. Click **Deploy**. Your dashboard is live with SSL and automatic git deploys!

---

## 🛠️ Troubleshooting & Known Limitations

| Symptom | Cause | Solution |
| :--- | :--- | :--- |
| **Wholesale: "Connection refused"** | `api.data.gov.in` rejects non-Indian IPs (e.g. GitHub runner). | Use Windows Task Scheduler on India PC or drop manual CSV into `data_inbox/`. |
| **Agmarknet API: HTTP 400 CAPTCHA_REQUIRED** | Agmarknet 2.0 API is strictly CAPTCHA-gated. | Captcha bypass is prohibited. Use manual downloads via `csv_import` adapter. |
| **Database error cached for 10m** | Old version cached empty DataFrame. | Upgraded in v2.0: `run_query` re-raises on failure and never caches query errors. |
| **Anomalies missing for new crop** | Insufficient baseline (<5 days). | Engine marks low-history rows as `confidence_level = 'low'` instead of dropping them. |

---

## ⚖️ Data Licensing & Mandatory Attribution

- **Wholesale Price Data**:
  - Sourced courtesy of **[agmarknet.gov.in](https://agmarknet.gov.in)** (Directorate of Marketing & Inspection, Ministry of Agriculture & Farmers Welfare, Government of India).
  - Open data accessed via [data.gov.in](https://data.gov.in) under the **Government Open Data License - India (GODL)**.
  - Per Agmarknet copyright terms: *"Source: agmarknet.gov.in must be explicitly acknowledged whenever data is reproduced or displayed."*
- **Retail Price Data**:
  - Quick-commerce consumer pricing monitored for Ahmedabad and major Gujarat cities.
  - Official retail price reports accessed from the **Department of Consumer Affairs (DCA)** Price Monitoring Division at [fcainfoweb.nic.in](https://fcainfoweb.nic.in). Modeled estimates are clearly distinguished from observed data.
