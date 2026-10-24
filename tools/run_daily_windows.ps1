# KrishiPulse — Daily Ingestion Runner for Windows Task Scheduler
# Executes all daily pipeline stages from your local India workstation.

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir "windows_runner.log"
$Timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")

Add-Content -Path $LogFile -Value "=========================================="
Add-Content -Path $LogFile -Value "[$Timestamp] Starting KrishiPulse Daily Pipeline"

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

# 1. Wholesale Ingestion (checks data_inbox/ first, falls back to data.gov.in)
Add-Content -Path $LogFile -Value "--- Step 1: Wholesale Ingestion ---"
& $Python pipeline\run_ingestion.py *>> $LogFile
$WholesaleExit = $LASTEXITCODE

# 2. Retail Manual & Google Sheet Ingestion
Add-Content -Path $LogFile -Value "--- Step 2: Retail Manual Ingestion ---"
& $Python ingestion\load_retail_manual.py *>> $LogFile

# 3. Generate Modeled Retail Prices
Add-Content -Path $LogFile -Value "--- Step 3: Retail Model Generation ---"
& $Python ingestion\generate_retail_model.py *>> $LogFile

# 4. Anomaly Detection Engine
Add-Content -Path $LogFile -Value "--- Step 4: Anomaly Detection Engine ---"
& $Python pipeline\run_anomaly_detection.py *>> $LogFile

# 5. Anomaly Alerts (Telegram / Email)
Add-Content -Path $LogFile -Value "--- Step 5: Anomaly Alerts ---"
& $Python pipeline\alerts.py *>> $LogFile

$EndTimestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $LogFile -Value "[$EndTimestamp] Pipeline Finished (Wholesale Exit: $WholesaleExit)"
Add-Content -Path $LogFile -Value "=========================================="
