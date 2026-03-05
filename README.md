# Claude Autopilot

A task queue and runner for the Claude CLI. Queue up work, let Claude process it autonomously in the background.

## Features

- Web dashboard for creating, prioritizing, and monitoring tasks
- Session persistence — resume conversations with follow-up messages
- Rate limit detection with automatic pause/retry
- Schedule window — run tasks only during specific hours (e.g., overnight while you sleep)
- Usage buffer — stops early so your rate limit recovers before you wake up
- Concurrent task limiting
- Basic auth for remote access

## Quick Start (Local)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Open [http://localhost:5055](http://localhost:5055).

### Hosting on a subpath

If you're hosting behind a reverse proxy on a subpath (e.g., `/autopilot`), set the `URL_PREFIX` env var:

```bash
URL_PREFIX=/autopilot python3 app.py
```

Or in your systemd service: `Environment=URL_PREFIX=/autopilot`

## Schedule Window

By default, autopilot only processes tasks between **11 PM and 4:30 AM** (configurable in Settings). The 2.5-hour buffer before the 7 AM end time lets ~50% of Claude's 5-hour rolling rate limit window recover before you start working.

| Setting | Default | Description |
|---------|---------|-------------|
| Start | 23:00 | When autopilot begins processing |
| End | 07:00 | When you want full rate limit availability |
| Buffer | 2.5h | Hours before End to stop (for rate limit recovery) |

The schedule window is optional — toggle it on in Settings when you want it.

## Server Deployment

See [ubuntu-setup.md](ubuntu-setup.md) for full Ubuntu/DigitalOcean deployment with systemd, nginx, and SSL.

```bash
git clone https://github.com/eotten/claude-autopilot.git /opt/claude-autopilot
```

## Updating

Pull the latest code and restart the service. Your settings and tasks are stored in a separate SQLite database and are not affected by code updates.

```bash
cd /opt/claude-autopilot
git pull origin main
sudo systemctl restart claude-autopilot
```

New settings are added automatically on restart with sensible defaults — existing settings and task history are preserved.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tasks` | GET | List all tasks |
| `/api/tasks` | POST | Create a task |
| `/api/tasks/<id>/run` | POST | Run a task immediately |
| `/api/tasks/<id>/stop` | POST | Stop a running task |
| `/api/tasks/<id>/follow-up` | POST | Send a follow-up message |
| `/api/tasks/<id>/complete` | POST | Mark task as completed |
| `/api/tasks/<id>/status` | POST | Update task status (used by Claude) |
| `/api/settings` | GET/POST | Read/update settings |
| `/api/queue/status` | GET | Queue and schedule status |
| `/api/queue/process` | POST | Trigger queue processing |
