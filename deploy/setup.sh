#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────
# Claude Autopilot — Digital Ocean Droplet Setup
# Run as root on a fresh Ubuntu 22.04/24.04 droplet:
#   curl -sSL <raw-url> | bash
# ─────────────────────────────────────────────────────────

DOMAIN="${1:-}"
APP_DIR="/opt/claude-autopilot"
DATA_DIR="/var/lib/claude-autopilot"
APP_USER="autopilot"

echo "=== Claude Autopilot Setup ==="

# ── 1. System packages ───────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx curl git

# ── 2. Create app user ───────────────────────────────────
echo "[2/7] Creating app user..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

# ── 3. Install Claude CLI ────────────────────────────────
echo "[3/7] Installing Claude CLI..."
if ! command -v claude &>/dev/null; then
    curl -fsSL https://cli.anthropic.com/install.sh | sh
    # Make claude available to the autopilot user
    ln -sf /root/.claude/bin/claude /usr/local/bin/claude 2>/dev/null || true
fi

echo ""
echo "  !! IMPORTANT: You need to authenticate Claude CLI as the $APP_USER user:"
echo "     sudo -u $APP_USER claude"
echo "  Log in, then exit. This only needs to be done once."
echo ""

# ── 4. Deploy app code ───────────────────────────────────
echo "[4/7] Deploying app..."
mkdir -p "$APP_DIR" "$DATA_DIR"

# Copy app files (assumes you've copied them to /tmp/claude-autopilot or cloned a repo)
if [ -d "/tmp/claude-autopilot" ]; then
    cp -r /tmp/claude-autopilot/* "$APP_DIR/"
else
    echo "  Put your app files in $APP_DIR manually, or:"
    echo "  scp -r ./claude-autopilot/* root@your-droplet:$APP_DIR/"
fi

# Create venv and install deps
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"

# ── 5. Generate auth credentials ─────────────────────────
echo "[5/7] Setting up auth..."
GENERATED_PASS=$(openssl rand -base64 16)
echo ""
echo "  Generated credentials:"
echo "    Username: admin"
echo "    Password: $GENERATED_PASS"
echo ""
echo "  Save these! They won't be shown again."
echo ""

# ── 6. Systemd service ───────────────────────────────────
echo "[6/7] Installing systemd service..."
cat > /etc/systemd/system/claude-autopilot.service <<SERVICEEOF
[Unit]
Description=Claude Autopilot
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=AUTOPILOT_DB=$DATA_DIR/autopilot.db
Environment=AUTOPILOT_USER=admin
Environment=AUTOPILOT_PASS=$GENERATED_PASS
ExecStart=$APP_DIR/venv/bin/gunicorn \
    --bind 127.0.0.1:5055 \
    --workers 1 \
    --threads 4 \
    --timeout 300 \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable claude-autopilot
systemctl start claude-autopilot

# ── 7. Nginx ─────────────────────────────────────────────
echo "[7/7] Configuring nginx..."
cat > /etc/nginx/sites-available/claude-autopilot <<NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN:-_};

    location / {
        proxy_pass http://127.0.0.1:5055;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/claude-autopilot /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── SSL ───────────────────────────────────────────────────
if [ -n "$DOMAIN" ]; then
    echo ""
    echo "Setting up SSL for $DOMAIN..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || {
        echo "  Certbot failed — run manually: certbot --nginx -d $DOMAIN"
    }
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "  App:    http://${DOMAIN:-$(curl -s ifconfig.me)}:80"
echo "  Auth:   admin / $GENERATED_PASS"
echo "  Status: systemctl status claude-autopilot"
echo "  Logs:   journalctl -u claude-autopilot -f"
echo ""
echo "  Next steps:"
echo "  1. Authenticate Claude CLI:  sudo -u $APP_USER claude"
echo "  2. Set your working directory in the app settings"
echo "  3. Update the default system prompt"
echo ""
