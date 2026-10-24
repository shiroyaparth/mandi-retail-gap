# Windows Task Scheduler Setup (schtasks)

GitHub Actions runners are hosted outside India, meaning requests to Indian government portals (`api.data.gov.in` and `agmarknet.gov.in`) often fail with "Connection refused" or timeouts due to geo-blocking.

Running the ingestion pipeline on your local Indian Windows machine completely resolves this issue.

---

## 1. Quick Setup: One-Line Task Creation

Open **PowerShell** (Run as Administrator or standard user) and execute:

```powershell
# Schedule daily run at 09:30 AM IST (after morning mandi arrivals)
schtasks /create /tn "KrishiPulse_Daily_Ingest_Morning" `
    /tr "powershell.exe -ExecutionPolicy Bypass -File \"D:\project\krishipulse\mandi-retail-gap\tools\run_daily_windows.ps1\"" `
    /sc daily /st 09:30 /f

# Optional: Afternoon run at 15:00 PM IST (for afternoon closing prices)
schtasks /create /tn "KrishiPulse_Daily_Ingest_Afternoon" `
    /tr "powershell.exe -ExecutionPolicy Bypass -File \"D:\project\krishipulse\mandi-retail-gap\tools\run_daily_windows.ps1\"" `
    /sc daily /st 15:00 /f
```

---

## 2. Managing the Task

### Run Instantly (Manual Test)
```powershell
schtasks /run /tn "KrishiPulse_Daily_Ingest_Morning"
```

### Check Task Status & Next Run Time
```powershell
schtasks /query /tn "KrishiPulse_Daily_Ingest_Morning" /fo LIST /v
```

### View Execution Logs
The runner logs every run with timestamps to:
```powershell
Get-Content -Tail 50 "D:\project\krishipulse\mandi-retail-gap\logs\windows_runner.log"
```

### Delete Scheduled Task
```powershell
schtasks /delete /tn "KrishiPulse_Daily_Ingest_Morning" /f
schtasks /delete /tn "KrishiPulse_Daily_Ingest_Afternoon" /f
```

---

## 3. How It Works
The script `tools/run_daily_windows.ps1`:
1. Checks folder `data_inbox/` for any manual Agmarknet reports and ingests them first.
2. If `data_inbox/` is empty, fetches today's live wholesale data from `api.data.gov.in` (which works cleanly from India).
3. Reads the latest retail price sheet.
4. Generates modeled retail distribution prices.
5. Runs the spatial anomaly detection engine with 30-day time-based baseline.
6. Dispatches Telegram / Email alerts if any anomalies or retail spread wedges are flagged.
7. Logs all stdout/stderr to `logs/windows_runner.log`.
