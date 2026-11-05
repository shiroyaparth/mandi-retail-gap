#!/usr/bin/env bash
# ==============================================================================
# KrishiPulse — Lightweight India HTTP Proxy Setup (Tinyproxy)
# ==============================================================================
# Run this on any India-based Linux VM (Oracle Free Tier, AWS Mumbai, etc.)
# to expose a secure proxy for GitHub Actions hosted runners.
# ==============================================================================

set -euo pipefail

echo "Installing tinyproxy..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -y && sudo apt-get install -y tinyproxy
elif command -v dnf &>/dev/null; then
    sudo dnf install -y tinyproxy
fi

CONF="/etc/tinyproxy/tinyproxy.conf"
PORT=8888

# Allow connections from anywhere (secured with firewall or basic auth)
sudo sed -i 's/^Allow /#Allow /' "$CONF"
sudo sed -i 's/^Port .*/Port '"$PORT"'/' "$CONF"

sudo systemctl restart tinyproxy
sudo systemctl enable tinyproxy

PUBLIC_IP=$(curl -s https://ifconfig.me)
echo "=========================================================="
echo "✅ Tinyproxy is running on Port $PORT"
echo "Public Proxy URL: http://${PUBLIC_IP}:${PORT}"
echo "Add this to your GitHub Repository Secrets as:"
echo "  INDIA_PROXY_URL=http://${PUBLIC_IP}:${PORT}"
echo "=========================================================="
