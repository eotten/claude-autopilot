import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("AUTOPILOT_DB", os.path.join(os.path.dirname(__file__), "autopilot.db"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            session_id TEXT,
            claude_output TEXT DEFAULT '',
            working_directory TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            scheduled_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            pending_follow_up TEXT,
            paused_until TEXT
        );

        CREATE TABLE IF NOT EXISTS task_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            log_type TEXT DEFAULT 'info',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
    """)

    # Migrations for existing DBs
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN sort_order INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN pending_follow_up TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN paused_until TEXT")
    except sqlite3.OperationalError:
        pass

    # Default settings
    defaults = {
        "system_prompt": "You are processing a backlog task from Claude Autopilot. Complete the task described below. When done, report your status by calling the API.\n\nIMPORTANT: When you finish or encounter an issue, update your status by running:\ncurl -X POST http://localhost:5055/api/tasks/{task_id}/status -H 'Content-Type: application/json' -d '{\"status\": \"completed\", \"output\": \"<your summary>\"}'",
        "schedule_enabled": "false",
        "schedule_time": "09:00",
        "schedule_interval_minutes": "60",
        "max_concurrent_tasks": "1",
        "claude_path": "claude",
        "default_working_directory": os.environ.get("AUTOPILOT_WORKDIR", os.getcwd()),
        "schedule_window_enabled": "false",
        "schedule_window_start": "23:00",
        "schedule_window_end": "07:00",
        "schedule_buffer_hours": "2.5",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    # On startup, reset any stuck "running" tasks — their processes are gone
    stuck = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
    if stuck:
        conn.execute(
            "UPDATE tasks SET status = 'queued' WHERE status = 'running'"
        )

    conn.commit()
    conn.close()
