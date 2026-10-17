#!/bin/bash
set -e

# Bulk Mailer VPS Setup Script
# Run as root on the VPS: bash /home/mailer/mailer/deploy/setup.sh

PROJECT_DIR="/home/mailer/mailer"
VENV_DIR="$PROJECT_DIR/venv"
DOMAIN="mailer.leihhaushessen.de"

echo "=== Bulk Mailer VPS Setup ==="

# 1. Create venv + install deps
echo "[1/5] Python venv + dependencies..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/bulk/requirements.txt" -q 2>/dev/null || \
"$VENV_DIR/bin/pip" install fastapi 'uvicorn[standard]' jinja2 python-multipart requests pypdf boto3 Pillow -q
echo "    Done."

# 2. Test that the app can import
echo "[2/5] Testing app import..."
cd "$PROJECT_DIR"
"$VENV_DIR/bin/python" -c "from bulk.web.main import app; print('    Import OK')"

# 3. Install systemd service
echo "[3/5] Installing systemd service..."
cp "$PROJECT_DIR/deploy/bulkmailer.service" /etc/systemd/system/bulkmailer.service
systemctl daemon-reload
systemctl enable bulkmailer
systemctl restart bulkmailer
sleep 2

# Check if service is running
if systemctl is-active --quiet bulkmailer; then
    echo "    Service running."
else
    echo "    ERROR: Service failed! Check: journalctl -u bulkmailer -n 30"
    systemctl status bulkmailer --no-pager
    exit 1
fi

# 4. Test local connection
echo "[4/5] Testing local connection..."
sleep 1
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "    Local: HTTP 200 OK"
else
    echo "    WARNING: HTTP $HTTP_CODE — check: journalctl -u bulkmailer -n 30"
fi

# 5. Caddy reverse proxy
echo "[5/5] Caddy setup..."
if command -v caddy &>/dev/null; then
    # Stop any existing Caddy
    systemctl stop caddy 2>/dev/null || true

    # IMPORTANT: Cloudflare proxy (orange cloud) must be OFF for Caddy HTTPS
    # If CF proxy is ON, Caddy can't get Let's Encrypt cert.
    # Either: turn off CF proxy (grey cloud / DNS only)
    # Or: use port 80 only and let CF handle SSL

    cp "$PROJECT_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
    systemctl restart caddy
    sleep 2
    if systemctl is-active --quiet caddy; then
        echo "    Caddy running."
    else
        echo "    WARNING: Caddy failed — check: journalctl -u caddy -n 20"
        echo "    If using Cloudflare proxy (orange cloud), see note below."
    fi
else
    echo "    Caddy not installed. Install with:"
    echo "    apt install -y debian-keyring debian-archive-keyring apt-transport-https curl"
    echo "    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
    echo "    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list"
    echo "    apt update && apt install caddy"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Service:  systemctl status bulkmailer"
echo "Logs:     journalctl -u bulkmailer -f"
echo "Local:    curl http://127.0.0.1:8000/"
echo "Web:      https://$DOMAIN"
echo ""
echo "CLOUDFLARE NOTE:"
echo "  If you use CF proxy (orange cloud), Caddy can't get a Let's Encrypt cert."
echo "  Options:"
echo "    a) Turn OFF CF proxy (grey cloud = DNS only) → Caddy handles SSL"
echo "    b) Keep CF proxy ON → change Caddyfile to ':80 { reverse_proxy ... }'"
echo "       and CF handles SSL (Full mode in CF SSL settings)"
