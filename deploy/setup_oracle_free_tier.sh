#!/usr/bin/env bash
# ==============================================================================
# KrishiPulse — Oracle Cloud Always Free VM (Mumbai/Hyderabad) Setup Script
# ==============================================================================
# Sets up an automated, 24/7 cloud execution environment located in India.
# Ensures 100% automated daily wholesale & retail ingestion without laptop dependency.
#
# Supported OS: Ubuntu 22.04/24.04 LTS, Debian 12, Oracle Linux 8/9
# Architecture: AMD64 or ARM64 (Ampere A1)
# ==============================================================================

set -euo pipefail

echo "=========================================================="
echo "  🌾 KrishiPulse Cloud Automation Setup (Oracle Free Tier) "
echo "=========================================================="

# 1. Detect Package Manager and Install Dependencies
echo "[1/5] Installing system packages and Python..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-pip python3-venv git curl libpq-dev jq
elif command -v dnf &>/dev/null; then
    sudo dnf update -y
    sudo dnf install -y python3 python3-pip git curl postgresql-devel jq
fi

# 2. Project Directory Setup
PROJECT_DIR="${HOME}/krishipulse"
if [ ! -d "$PROJECT_DIR" ]; then
    echo "[2/5] Setting up project directory at $PROJECT_DIR..."
    git clone https://github.com/krishipulse/mandi-retail-gap.git "$PROJECT_DIR" || mkdir -p "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"

# 3. Create Python Virtual Environment
echo "[3/5] Configuring Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi

# 4. Setup .env Configuration if missing
if [ ! -f ".env" ]; then
    echo "[4/5] Creating template .env file..."
    cat << 'EOF' > .env
# KrishiPulse Cloud Environment
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
DATA_GOV_API_KEY=your_data_gov_api_key_here
RETAIL_SHEET_URL=your_google_sheet_csv_url_here
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
    echo "⚠️ Created .env template. Please edit ${PROJECT_DIR}/.env with your real credentials."
else
    echo "[4/5] Existing .env file found."
fi

# 5. Automation Mode Selection
echo "----------------------------------------------------------"
echo "Select Automation Mode for 24/7 Daily Execution:"
echo "  1) GitHub Actions Self-Hosted Runner (Recommended)"
echo "     - Managed via GitHub Actions UI / schedule"
echo "     - Secrets remain encrypted in GitHub Secrets"
echo "     - Runs natively in India to bypass all government geo-blocks"
echo ""
echo "  2) Native Systemd Timer"
echo "     - Completely independent of GitHub Actions"
echo "     - Triggers daily at 09:00 AM IST and 14:30 PM IST via local cron/systemd"
echo "----------------------------------------------------------"
read -rp "Enter choice [1 or 2] (default: 1): " CHOICE
CHOICE="${CHOICE:-1}"

if [ "$CHOICE" = "1" ]; then
    echo ""
    echo ">>> Setting up GitHub Actions Self-Hosted Runner..."
    echo "1. On GitHub, navigate to your repo -> Settings -> Actions -> Runners -> 'New self-hosted runner'"
    echo "2. Select OS: Linux, Architecture: x64 or ARM64 (matching this VM)"
    echo ""
    read -rp "Paste your GitHub Runner Registration Token: " RUNNER_TOKEN
    read -rp "Paste your GitHub Repo URL (e.g., https://github.com/youruser/mandi-retail-gap): " REPO_URL

    RUNNER_DIR="${HOME}/actions-runner"
    mkdir -p "$RUNNER_DIR" && cd "$RUNNER_DIR"

    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        RUNNER_ARCH="x64"
    elif [ "$ARCH" = "aarch64" ]; then
        RUNNER_ARCH="arm64"
    else
        RUNNER_ARCH="x64"
    fi

    # Fetch latest runner release version from GitHub API
    LATEST_VER=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | tr -d 'v')
    LATEST_VER="${LATEST_VER:-2.321.0}"
    TAR_NAME="actions-runner-linux-${RUNNER_ARCH}-${LATEST_VER}.tar.gz"

    if [ ! -f "$TAR_NAME" ]; then
        echo "Downloading ${TAR_NAME}..."
        curl -o "$TAR_NAME" -L "https://github.com/actions/runner/releases/download/v${LATEST_VER}/${TAR_NAME}"
        tar xzf "$TAR_NAME"
    fi

    ./config.sh --url "$REPO_URL" --token "$RUNNER_TOKEN" --labels "self-hosted,linux,india" --unattended --replace

    # Install runner as a systemd service so it auto-starts on reboot
    sudo ./svc.sh install
    sudo ./svc.sh start
    echo "✅ GitHub Actions Self-Hosted Runner installed and running as a systemd service!"

elif [ "$CHOICE" = "2" ]; then
    echo ""
    echo ">>> Installing Native Systemd Service and Timer..."
    sudo cp "${PROJECT_DIR}/deploy/krishipulse-daily.service" /etc/systemd/system/
    sudo cp "${PROJECT_DIR}/deploy/krishipulse-daily.timer" /etc/systemd/system/
    
    # Replace placeholder user with current user
    sudo sed -i "s|User=ubuntu|User=${USER}|g" /etc/systemd/system/krishipulse-daily.service
    sudo sed -i "s|/home/ubuntu/krishipulse|${PROJECT_DIR}|g" /etc/systemd/system/krishipulse-daily.service
    
    sudo systemctl daemon-reload
    sudo systemctl enable --now krishipulse-daily.timer
    echo "✅ Systemd daily timer enabled! Schedule: Daily at 09:00 AM IST & 14:30 PM IST."
fi

echo "=========================================================="
echo "🎉 KrishiPulse Cloud Setup Complete!"
echo "=========================================================="
