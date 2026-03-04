# Prompt to give Claude on the server

Run this on your server once Claude CLI is authenticated:

```bash
claude -p "$(cat <<'EOF'
Set up the Claude Autopilot Flask app on this Ubuntu server. The app files are already at /opt/claude-autopilot. Do the following:

1. Create a Python venv and install dependencies:
   python3 -m venv /opt/claude-autopilot/venv
   /opt/claude-autopilot/venv/bin/pip install -r /opt/claude-autopilot/requirements.txt

2. Create the data directory:
   mkdir -p /var/lib/claude-autopilot

3. Generate a random password and create a systemd service at /etc/systemd/system/claude-autopilot.service:
   - User=root
   - WorkingDirectory=/opt/claude-autopilot
   - Environment=AUTOPILOT_DB=/var/lib/claude-autopilot/autopilot.db
   - Environment=AUTOPILOT_USER=admin
   - Environment=AUTOPILOT_PASS=<the generated password>
   - Environment=AUTOPILOT_WORKDIR=/root
   - ExecStart=/opt/claude-autopilot/venv/bin/gunicorn --bind 127.0.0.1:5055 --workers 1 --threads 4 --timeout 300 app:app
   - Restart=always
   - RestartSec=5
   - WantedBy=multi-user.target

4. Enable and start the service:
   systemctl daemon-reload
   systemctl enable claude-autopilot
   systemctl start claude-autopilot

5. Set up nginx reverse proxy at /etc/nginx/sites-available/claude-autopilot:
   - Listen on port 80
   - server_name _
   - proxy_pass to http://127.0.0.1:5055
   - proxy_read_timeout 300s
   - Symlink to sites-enabled, remove default site, reload nginx

6. Verify everything works:
   - systemctl status claude-autopilot should show active
   - curl -u admin:<password> http://localhost:5055/api/queue/status should return JSON

Print the generated password at the end so I can save it.
EOF
)" --dangerously-skip-permissions
```
