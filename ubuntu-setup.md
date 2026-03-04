# Claude Autopilot — Ubuntu 24.04 Setup

## 1. System packages

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx curl git
```

## 2. Add swap (required for small Droplets)

The Claude CLI install will get OOM-killed on 1GB Droplets without swap.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h  # verify swap is active
```

## 3. Install Claude CLI

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

If the install gets killed even with swap, use npm instead:

```bash
apt install -y nodejs npm
npm install -g @anthropic-ai/claude-code
```

Add to PATH if needed:

```bash
echo 'export PATH="$HOME/.claude/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Authenticate (interactive — do this once):

```bash
claude
```

Log in, then exit.

## 3. Deploy the app

```bash
# Copy files to server (from your local machine)
# scp -r ./claude-autopilot root@your-server:/opt/claude-autopilot

# On the server
cd /opt/claude-autopilot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Create data directory
sudo mkdir -p /var/lib/claude-autopilot
sudo chown $USER:$USER /var/lib/claude-autopilot
```

## 4. Test it works

```bash
cd /opt/claude-autopilot
AUTOPILOT_DB=/var/lib/claude-autopilot/autopilot.db ./venv/bin/python3 app.py
```

Visit `http://your-server-ip:5055` — confirm it loads, then Ctrl+C.

## 5. Systemd service

```bash
sudo tee /etc/systemd/system/claude-autopilot.service > /dev/null <<'EOF'
[Unit]
Description=Claude Autopilot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/claude-autopilot
Environment=AUTOPILOT_DB=/var/lib/claude-autopilot/autopilot.db
Environment=AUTOPILOT_USER=admin
Environment=AUTOPILOT_PASS=CHANGE_THIS_PASSWORD
Environment=AUTOPILOT_WORKDIR=/root/projects
ExecStart=/opt/claude-autopilot/venv/bin/gunicorn \
    --bind 127.0.0.1:5055 \
    --workers 1 \
    --threads 4 \
    --timeout 300 \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Generate a password and update the service file:

```bash
PASS=$(openssl rand -base64 16)
echo "Your password: $PASS"
sudo sed -i "s/CHANGE_THIS_PASSWORD/$PASS/" /etc/systemd/system/claude-autopilot.service
```

Start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable claude-autopilot
sudo systemctl start claude-autopilot
sudo systemctl status claude-autopilot
```

## 6. Nginx reverse proxy

```bash
sudo tee /etc/nginx/sites-available/claude-autopilot > /dev/null <<'EOF'
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5055;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/claude-autopilot /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 7. SSL (optional, requires a domain pointed at the server)

```bash
sudo certbot --nginx -d your-domain.com
```

## Common commands

```bash
# View logs
sudo journalctl -u claude-autopilot -f

# Restart after code changes
sudo systemctl restart claude-autopilot

# Update code
cd /opt/claude-autopilot
git pull  # or scp new files
sudo systemctl restart claude-autopilot

# Check if running
curl -u admin:YOUR_PASS http://localhost:5055/api/queue/status
```

## Notes

- **Workers = 1** is intentional. The scheduler and process tracker use in-memory state, so multiple workers would break things.
- **Threads = 4** handles concurrent HTTP requests while keeping a single process.
- Claude CLI must be authenticated for the same user running the service (root in this example). If you use a different user, authenticate as that user first.
- The SQLite DB lives at `/var/lib/claude-autopilot/autopilot.db`. Back this up if you care about task history.
- Basic auth protects the UI. For IP-based access control, add `allow`/`deny` rules in the nginx config.
