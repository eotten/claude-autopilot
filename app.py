import os
import secrets
from functools import wraps
from flask import Flask, request, jsonify, render_template, redirect, url_for, Response
from database import get_db, init_db
from scheduler import start_scheduler, run_task, stop_task, process_queue, _running_processes, is_within_schedule_window

app = Flask(__name__)

# Always init DB at import time so the reloader child process has tables
init_db()

# Basic auth — set AUTOPILOT_USER and AUTOPILOT_PASS env vars to enable
AUTH_USER = os.environ.get("AUTOPILOT_USER")
AUTH_PASS = os.environ.get("AUTOPILOT_PASS")


def check_auth(username, password):
    return secrets.compare_digest(username, AUTH_USER) and secrets.compare_digest(password, AUTH_PASS)


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTH_USER:
            return f(*args, **kwargs)  # Auth disabled if env vars not set
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="Claude Autopilot"'},
            )
        return f(*args, **kwargs)
    return decorated


@app.before_request
def _auth_guard():
    if not AUTH_USER:
        return
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return Response(
            "Login required", 401,
            {"WWW-Authenticate": 'Basic realm="Claude Autopilot"'},
        )


# ──────────────────────────────────────
# Pages
# ──────────────────────────────────────


@app.route("/")
def dashboard():
    conn = get_db()
    active = conn.execute(
        "SELECT * FROM tasks WHERE status IN ('running', 'queued', 'paused') ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 WHEN 'queued' THEN 2 END, sort_order ASC, priority DESC, created_at ASC"
    ).fetchall()
    review = conn.execute(
        "SELECT * FROM tasks WHERE status = 'review' ORDER BY completed_at DESC"
    ).fetchall()
    completed = conn.execute(
        "SELECT * FROM tasks WHERE status IN ('completed', 'failed', 'cancelled') ORDER BY completed_at DESC LIMIT 50"
    ).fetchall()
    settings = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT * FROM settings").fetchall()
    }
    conn.close()
    return render_template("dashboard.html", active=active, review=review, completed=completed, settings=settings)


@app.route("/task/<int:task_id>")
def task_detail(task_id):
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    messages = conn.execute(
        "SELECT * FROM task_messages WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    logs = conn.execute(
        "SELECT * FROM task_logs WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    conn.close()
    if not task:
        return redirect(url_for("dashboard"))
    return render_template("task_detail.html", task=task, messages=messages, logs=logs)


@app.route("/archive")
def archive():
    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE status = 'archived' ORDER BY completed_at DESC, created_at DESC"
    ).fetchall()
    conn.close()
    return render_template("archive.html", tasks=tasks)


# ──────────────────────────────────────
# Task API
# ──────────────────────────────────────


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    conn = get_db()
    tasks = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(t) for t in tasks])


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.json or request.form
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    conn = get_db()
    # Get next sort_order
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM tasks WHERE status = 'queued'").fetchone()[0]
    cursor = conn.execute(
        "INSERT INTO tasks (title, description, priority, sort_order, working_directory, scheduled_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            title,
            data.get("description", ""),
            int(data.get("priority", 0)),
            max_order + 1,
            data.get("working_directory", ""),
            data.get("scheduled_at") or None,
        ),
    )
    conn.commit()
    task_id = cursor.lastrowid
    conn.close()

    if request.headers.get("Accept") == "application/json" or request.is_json:
        return jsonify({"id": task_id, "status": "queued"}), 201
    return redirect(url_for("dashboard"))


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    conn = get_db()
    conn.execute("DELETE FROM task_messages WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/archive", methods=["POST"])
def archive_task(task_id):
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status = 'archived' WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": "archived"})


@app.route("/api/tasks/<int:task_id>/run", methods=["POST"])
def run_task_now(task_id):
    """Manually trigger a task to run immediately."""
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if task["status"] == "running":
        return jsonify({"error": "Task already running"}), 400

    conn = get_db()
    conn.execute("UPDATE tasks SET status = 'queued' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    run_task(task_id)
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/tasks/<int:task_id>/follow-up", methods=["POST"])
def follow_up_task(task_id):
    """Send a follow-up message to resume a task's session."""
    data = request.json
    if not data or not data.get("message", "").strip():
        return jsonify({"error": "Message is required"}), 400

    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if task["status"] == "running":
        return jsonify({"error": "Task already running"}), 400

    message = data["message"].strip()
    run_now = data.get("run_now", True)

    if run_now:
        run_task(task_id, follow_up_message=message)
        return jsonify({"ok": True, "status": "running"})
    else:
        # Queue the follow-up for later — store the message so the scheduler
        # can resume the session with it when it's picked up
        conn = get_db()
        conn.execute(
            "UPDATE tasks SET status = 'queued', pending_follow_up = ? WHERE id = ?",
            (message, task_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "status": "queued"})


@app.route("/api/tasks/<int:task_id>/complete", methods=["POST"])
def mark_complete(task_id):
    """Mark a task as fully completed."""
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": "completed"})


@app.route("/api/tasks/<int:task_id>/reorder", methods=["POST"])
def reorder_task(task_id):
    """Move a task up or down in the queue."""
    data = request.json
    direction = data.get("direction", "up")

    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task or task["status"] != "queued":
        conn.close()
        return jsonify({"error": "Task not found or not queued"}), 400

    current_order = task["sort_order"] or 0

    if direction == "up":
        # Find the task above
        above = conn.execute(
            "SELECT id, sort_order FROM tasks WHERE status = 'queued' AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
            (current_order,),
        ).fetchone()
        if above:
            conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (current_order, above["id"]))
            conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (above["sort_order"], task_id))
    else:
        # Find the task below
        below = conn.execute(
            "SELECT id, sort_order FROM tasks WHERE status = 'queued' AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
            (current_order,),
        ).fetchone()
        if below:
            conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (current_order, below["id"]))
            conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (below["sort_order"], task_id))

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ──────────────────────────────────────
# Stop / Live Output
# ──────────────────────────────────────


@app.route("/api/tasks/<int:task_id>/stop", methods=["POST"])
def stop_task_now(task_id):
    if stop_task(task_id):
        return jsonify({"ok": True, "status": "cancelled"})
    return jsonify({"error": "Task not running"}), 400


@app.route("/api/tasks/<int:task_id>/output", methods=["GET"])
def get_task_output(task_id):
    conn = get_db()
    task = conn.execute(
        "SELECT claude_output, status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    conn.close()
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"output": task["claude_output"] or "", "status": task["status"]})


# ──────────────────────────────────────
# Claude Status Reporting API
# ──────────────────────────────────────


@app.route("/api/tasks/<int:task_id>/status", methods=["POST"])
def update_task_status(task_id):
    data = request.json
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({"error": "Task not found"}), 404

    new_status = data.get("status", task["status"])
    output = data.get("output", "")
    log_message = data.get("log", "")

    updates = []
    params = []

    if new_status and new_status != task["status"]:
        updates.append("status = ?")
        params.append(new_status)
        if new_status in ("completed", "failed"):
            updates.append("completed_at = datetime('now')")

    if output:
        updates.append("claude_output = ?")
        params.append(output)

    if updates:
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)

    if log_message:
        conn.execute(
            "INSERT INTO task_logs (task_id, message, log_type) VALUES (?, ?, ?)",
            (task_id, log_message, data.get("log_type", "info")),
        )

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "task_id": task_id, "status": new_status})


# ──────────────────────────────────────
# Settings API
# ──────────────────────────────────────


@app.route("/api/settings", methods=["GET"])
def get_settings():
    conn = get_db()
    settings = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT * FROM settings").fetchall()
    }
    conn.close()
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.json or request.form
    conn = get_db()
    for key, value in data.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

    if request.headers.get("Accept") == "application/json" or request.is_json:
        return jsonify({"ok": True})
    return redirect(url_for("dashboard"))


# ──────────────────────────────────────
# Queue control
# ──────────────────────────────────────


@app.route("/api/queue/process", methods=["POST"])
def trigger_queue():
    process_queue()
    return jsonify({"ok": True})


@app.route("/api/queue/status", methods=["GET"])
def queue_status():
    conn = get_db()
    paused_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'paused'").fetchone()[0]
    paused_until = None
    if paused_count:
        row = conn.execute("SELECT MAX(paused_until) FROM tasks WHERE status = 'paused'").fetchone()
        paused_until = row[0] if row else None
    conn.close()
    in_window = is_within_schedule_window()
    return jsonify({
        "running_tasks": list(_running_processes.keys()),
        "running_count": len(_running_processes),
        "paused_count": paused_count,
        "paused_until": paused_until,
        "in_schedule_window": in_window,
    })


# ──────────────────────────────────────
# Boot
# ──────────────────────────────────────

# Start scheduler for both dev (flask run) and prod (gunicorn)
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5055, debug=True, use_reloader=False)
