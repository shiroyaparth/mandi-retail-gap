# KrishiPulse — Cloud Automation & Zero-Laptop Execution Guide

This document explains how to achieve **100% automated daily data collection, processing, anomaly detection, and dashboard updates** for KrishiPulse without requiring your personal laptop to be open or powered on.

---

## 1. The Root Infrastructure Reality: India IP Geo-fencing

### Verified Technical Facts:
1. **Indian Government Portals (`api.data.gov.in` & `agmarknet.gov.in`)**:
   - The National Informatics Centre (NIC) and Open Government Data (OGD) firewalls **restrict or refuse TCP/TLS connections originating from non-Indian IP blocks**.
   - Standard GitHub-hosted runners (`ubuntu-latest`) execute inside Microsoft Azure datacenters located in the United States, United Kingdom, and the Netherlands.
   - When GitHub Actions calls `api.data.gov.in` directly from standard runners, requests frequently fail with `Connection refused (ECONNREFUSED)` or `Read timed out`.
2. **Upstream Government Server Reliability**:
   - Even from within India, government APIs frequently throw transient `HTTP 502 Bad Gateway` or `HTTP 504 Gateway Timeout` during peak morning traffic.
   - **KrishiPulse Rule**: A failed fetch is **never** treated as "no data" and **never** deletes or corrupts existing records.

---

## 2. The Recommended Architecture: Oracle Cloud Always Free (India)

To make data collection run 24/7 without your laptop, we run the execution from a machine with an **authentic Indian IP address**.

The cleanest, 100% free solution is an **Oracle Cloud Always Free VM** located in **Mumbai** or **Hyderabad**.

### Why Oracle Cloud Always Free?
| Feature | Oracle Cloud Always Free | AWS / GCP / Azure |
| :--- | :--- | :--- |
| **Data Center in India** | Yes (`ap-mumbai-1` / `ap-hyderabad-1`) | Yes (Mumbai) |
| **Monthly Cost** | **$0.00 / month forever** | Free for 12 months, then paid |
| **IP Address** | Dedicated Indian Public IPv4 | Varies |
| **Resources** | Up to 4 ARM Ampere cores / 24 GB RAM | 1 vCPU / 1 GB RAM |
| **Laptop Needed?** | **No** (Runs 24/7 in cloud) | **No** |

---

## 3. Step-by-Step Setup: 10-Minute Deployment

### Step A: Create Your Free VM (One-time)
1. Go to [cloud.oracle.com](https://cloud.oracle.com) and create an **Always Free** account.
2. Select your Home Region as **India West (Mumbai)** (`ap-mumbai-1`) or **India South (Hyderabad)** (`ap-hyderabad-1`).
3. From the Oracle Cloud Console:
   - Navigate to **Compute** -> **Instances** -> **Create Instance**.
   - Name: `krishipulse-runner`.
   - Image: **Ubuntu 22.04 LTS** or **Oracle Linux 9** (Always Free-eligible).
   - Shape: `VM.Standard.E2.1.Micro` (AMD, Always Free) OR `VM.Standard.A1.Flex` (Ampere ARM, Always Free).
   - Networking: Assign a Public IPv4 address.
   - Add your SSH public key.
   - Click **Create**.

---

### Step B: Run the One-Click Deployment Script
SSH into your Oracle VM:
```bash
ssh -i /path/to/your/private_key ubuntu@<YOUR_VM_PUBLIC_IP>
```

Clone the repository and run the automated setup script:
```bash
git clone https://github.com/<your_username>/mandi-retail-gap.git ~/krishipulse
cd ~/krishipulse
chmod +x deploy/setup_oracle_free_tier.sh
./deploy/setup_oracle_free_tier.sh
```

The script will prompt you for your preferred automation mode:

#### Option 1: GitHub Actions Self-Hosted Runner (Recommended ⭐)
* Keeps all automation management, secrets, logs, and `workflow_dispatch` buttons centralized in GitHub Actions.
* Secrets (`DATABASE_URL`, `DATA_GOV_API_KEY`) stay encrypted in GitHub Secrets.
* How to register:
  1. In your GitHub repository, open **Settings** -> **Actions** -> **Runners**.
  2. Click **New self-hosted runner**.
  3. Copy the token provided by GitHub and paste it when prompted by the script.
  4. The script automatically installs the runner as an auto-starting `systemd` service.

#### Option 2: Native Systemd Timer
* If you do not wish to use GitHub Actions, the script installs `krishipulse-daily.service` and `krishipulse-daily.timer` directly on the VM.
* Executes automatically every day at **09:00 AM IST** and **14:30 PM IST**.

---

## 4. Configuring GitHub Actions Secrets

In your GitHub repository, go to **Settings** -> **Secrets and variables** -> **Actions** and add:

| Secret Name | Description | Example / Note |
| :--- | :--- | :--- |
| `DATABASE_URL` | Neon PostgreSQL connection string | `postgresql://user:pass@ep-xyz.neon.tech/neondb?sslmode=require` |
| `DATA_GOV_API_KEY` | Your data.gov.in API key | From `api.data.gov.in` |
| `RETAIL_SHEET_URL` | Published Google Sheet CSV URL | URL ending with `output=csv` |
| `TELEGRAM_BOT_TOKEN` | (Optional) Telegram bot token | From `@BotFather` |
| `TELEGRAM_CHAT_ID` | (Optional) Telegram user or channel ID | Numeric Telegram chat ID |
| `INDIA_PROXY_URL` | (Optional) Forward proxy URL | `http://<india_vm_ip>:8888` if using `ubuntu-latest` |

---

## 5. Daily Execution Timeline (Zero-Laptop Daily Routine)

```mermaid
sequenceDiagram
    autonumber
    actor Friend as Friend / Collaborator
    participant Sheet as Google Sheet
    participant OCI as Oracle Cloud India VM
    participant Gov as api.data.gov.in
    participant DB as Neon PostgreSQL
    participant Bot as Telegram Bot
    participant Web as Streamlit Dashboard

    Friend->>Sheet: Enters 5 retail prices on phone (09:00 AM IST)
    Note over OCI: 09:00 AM IST Schedule Triggers
    OCI->>Gov: Ingest wholesale arrivals via Indian IP
    Gov-->>DB: Upsert wholesale rates (Rs/kg) & audit log
    OCI->>Sheet: Pull fresh retail prices (data_type='scraped')
    Sheet-->>DB: Upsert retail records
    OCI->>DB: Compute regional retail model (Surat, Rajkot, etc.)
    OCI->>DB: Execute spatial anomaly engine (30d baseline & wedges)
    opt Anomaly or Degradation Detected
        OCI->>Bot: Send Telegram alert with explanation
    end
    Web->>DB: Streamlit app renders fresh data (Always Available)
```

1. **09:00 AM IST**: Your friend spends 60 seconds entering today's 5 retail prices in Google Sheets from their phone.
2. **09:00 AM IST**: GitHub Actions triggers the self-hosted Indian runner (or systemd timer on Oracle VM).
3. **Wholesale Step**: Fetches today's Gujarat mandi arrivals for Onion, Potato, Tomato, Garlic, Green Chilli through Indian IP, validates prices, and upserts into Neon PostgreSQL.
4. **Retail Step**: Ingests Google Sheet prices and generates modeled estimates for Surat, Vadodara, Rajkot, Gandhinagar, Bhavnagar.
5. **Anomaly Step**: Calculates 30-day moving baselines, Z-scores, and retail markup wedges.
6. **Alert Step**: Sends an instant Telegram or Email message if a price spike or extreme markup (>120%) is detected.
7. **Streamlit**: Hosted on Streamlit Community Cloud (free) or locally; reads directly from Neon PostgreSQL.

---

## 6. Failure Recovery & Fallback Strategies

### Scenario 1: `api.data.gov.in` is temporarily down (HTTP 502 / 504)
* **What happens**: The pipeline automatically retries 4 times with exponential backoff (2s, 4s, 8s).
* **Failure isolation**: If the API remains down, the workflow sends a failure notification alert on Telegram/email:
  > *⚠️ Wholesale API connection failed or timed out. Existing data preserved.*
* **Historical data protected**: Existing wholesale records in PostgreSQL are untouched.
* **Retail pipeline continues**: Retail sheet ingestion, regional modeling, and anomaly calculation still run.
* **Dashboard transparency**: Streamlit shows the amber alert banner informing users that data is from the last available date.

### Scenario 2: Manual Agmarknet File Drop
If the government API is down for multiple days:
1. Download the date-wise report from [agmarknet.gov.in](https://agmarknet.gov.in).
2. Drop the CSV or Excel file into `data_inbox/`.
3. In GitHub Actions, click **Run workflow** -> Select source: `csv_import` (or run locally).
4. The importer auto-detects column names (including date-stamped headers like `Modal Price 26-09-2026 to 22-10-2026`), converts quintals to ₹/kg, and backfills the database with zero duplicates.

---

## 7. Operational Checklist

- [x] Multi-source ingestion runner (`pipeline/run_ingestion.py`) configured with retry & backoff.
- [x] Dynamic Agmarknet CSV reader (`pipeline/sources/csv_import.py`) supports all web report formats.
- [x] GitHub Actions workflow (`.github/workflows/daily_ingestion.yml`) configured for 09:00 AM IST schedule.
- [x] Oracle Cloud setup script (`deploy/setup_oracle_free_tier.sh`) ready for single-command installation.
- [x] Immediate failure alert dispatches via Telegram / Email on API degradation.
- [x] Idempotent upserts prevent duplicate records across all wholesale and retail tables.
- [x] Dashboard (`app.py`) displays multi-crop trends, anomaly flags, and data health audits.
