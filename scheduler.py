import subprocess
import threading
import json
import os
import re
import signal
from datetime import datetime, timedelta
from database import get_db

_scheduler_thread = None
_stop_event = threading.Event()
# Stores {"process": Popen, "thread": Thread} per task_id
_running_processes = {}


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


_RATE_LIMIT_PATTERNS = [
    r"rate.?limit",
    r"too many requests",
    r"token limit",
    r"usage limit",
    r"capacity",
    r"try again",
    r"429",
    r"overloaded",
]

# Default pause: 30 minutes. Can be overridden by parsing the error.
_DEFAULT_PAUSE_MINUTES = 30


def _is_rate_limited(text):
    """Check if text contains rate limit indicators."""
    lower = text.lower()
    return any(re.search(pat, lower) for pat in _RATE_LIMIT_PATTERNS)


def _parse_wait_minutes(text):
    """Try to extract a wait time from the error message. Returns minutes or default."""
    # Look for patterns like "try again in 30 minutes", "wait 1 hour", "retry after 45m"
    m = re.search(r"(\d+)\s*(?:minute|min|m)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:hour|hr|h)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*(?:second|sec|s)\b", text, re.IGNORECASE)
    if m:
        return max(1, int(m.group(1)) // 60)
    return _DEFAULT_PAUSE_MINUTES


def _pause_for_rate_limit(task_id, stderr_text, is_resume=False, follow_up_message=None):
    """Pause this task and all queued tasks until the rate limit window passes."""
    wait_minutes = _parse_wait_minutes(stderr_text)
    pause_until = (datetime.utcnow() + timedelta(minutes=wait_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    # Pause the task that hit the limit — keep its follow-up so it can resume
    if is_resume and follow_up_message:
        conn.execute(
            "UPDATE tasks SET status = 'paused', paused_until = ?, pending_follow_up = ? WHERE id = ?",
            (pause_until, follow_up_message, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = 'paused', paused_until = ? WHERE id = ?",
            (pause_until, task_id),
        )

    # Also pause all queued tasks so they don't immediately burn through the limit
    conn.execute(
        "UPDATE tasks SET status = 'paused', paused_until = ? WHERE status = 'queued'",
        (pause_until,),
    )
    conn.commit()
    conn.close()

    _add_log(task_id, f"Rate limited — paused until {pause_until} (~{wait_minutes}min)", "error")


def _set_output(task_id, text):
    """Set the full output for a task."""
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET claude_output = ? WHERE id = ?",
        (text, task_id),
    )
    conn.commit()
    conn.close()


def _add_log(task_id, message, log_type="info"):
    conn = get_db()
    conn.execute(
        "INSERT INTO task_logs (task_id, message, log_type) VALUES (?, ?, ?)",
        (task_id, message, log_type),
    )
    conn.commit()
    conn.close()


def _add_message(task_id, role, content):
    """Store a conversation turn."""
    conn = get_db()
    conn.execute(
        "INSERT INTO task_messages (task_id, role, content) VALUES (?, ?, ?)",
        (task_id, role, content),
    )
    conn.commit()
    conn.close()


def _process_stream(task_id, proc, append=False):
    """Process stream-json output from Claude CLI. Returns collected text."""
    conn = get_db()
    if not append:
        existing = ""
    else:
        row = conn.execute("SELECT claude_output FROM tasks WHERE id = ?", (task_id,)).fetchone()
        existing = (row["claude_output"] or "") + "\n\n---\n\n" if row and row["claude_output"] else ""
    conn.close()

    collected_text = []
    session_id = None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        # Capture session_id from init event
        if event_type == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if session_id:
                conn = get_db()
                conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
                conn.commit()
                conn.close()

        # Extract text from assistant messages
        elif event_type == "assistant":
            msg = event.get("message", {})
            # Also capture session_id from assistant messages
            if not session_id:
                session_id = event.get("session_id")
                if session_id:
                    conn = get_db()
                    conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
                    conn.commit()
                    conn.close()

            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        collected_text.append(text)
                        _set_output(task_id, existing + "\n".join(collected_text))

        # Extract final result text
        elif event_type == "result":
            session_id = event.get("session_id") or session_id
            if session_id:
                conn = get_db()
                conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
                conn.commit()
                conn.close()

            result_text = event.get("result", "")
            if result_text and not collected_text:
                collected_text.append(result_text)
                _set_output(task_id, existing + result_text)
            cost = event.get("total_cost_usd")
            if cost:
                _add_log(task_id, f"Cost: ${cost:.4f}")

        # Log tool use
        elif event_type == "tool_use":
            tool_name = event.get("tool", event.get("name", "unknown"))
            _add_log(task_id, f"Using tool: {tool_name}")

    return collected_text, session_id


def run_task(task_id, follow_up_message=None):
    """Run a task via Claude CLI. If follow_up_message is provided, resumes the session."""
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return

    claude_path = get_setting("claude_path", "claude")
    working_dir = task["working_directory"] or get_setting(
        "default_working_directory", "."
    )

    is_resume = follow_up_message and task["session_id"]

    system_prompt = get_setting("system_prompt", "")

    if is_resume:
        prompt = f"{system_prompt}\n\n---\n\n{follow_up_message}" if system_prompt else follow_up_message
        prompt = prompt.replace("{task_id}", str(task_id))
        _add_message(task_id, "user", follow_up_message)
    else:
        prompt = f"{system_prompt}\n\n---\n\nTASK: {task['title']}\n\n{task['description']}"
        prompt = prompt.replace("{task_id}", str(task_id))
        _add_message(task_id, "user", task["title"] + ("\n\n" + task["description"] if task["description"] else ""))

    # Mark as running and clear any pending follow-up
    conn.execute(
        "UPDATE tasks SET status = 'running', started_at = datetime('now'), completed_at = NULL, pending_follow_up = NULL WHERE id = ?",
        (task_id,),
    )
    if not is_resume:
        conn.execute("UPDATE tasks SET claude_output = '' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    _add_log(task_id, "Resuming session" if is_resume else "Task started via Claude CLI")

    def _run():
        proc = None
        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)

            cmd = [claude_path, "-p", prompt, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
            if is_resume:
                cmd.extend(["--resume", task["session_id"]])

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=working_dir,
                env=env,
                preexec_fn=os.setsid,
            )

            _running_processes[task_id]["process"] = proc

            collected_text, session_id = _process_stream(task_id, proc, append=is_resume)

            # Store assistant response as message
            if collected_text:
                _add_message(task_id, "assistant", "\n".join(collected_text))

            proc.wait()
            stderr = proc.stderr.read()

            conn = get_db()
            current = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()

            if current and current["status"] == "cancelled":
                _add_log(task_id, "Task was cancelled by user")
                conn.close()
                return

            # Check for rate limiting in stderr or collected output
            all_output = (stderr or "") + " " + " ".join(collected_text)
            if proc.returncode != 0 and _is_rate_limited(all_output):
                conn.close()
                _pause_for_rate_limit(task_id, all_output, is_resume=is_resume, follow_up_message=follow_up_message)
                return

            if proc.returncode != 0:
                if stderr:
                    err_msg = stderr.strip()[:500]
                    _add_log(task_id, f"STDERR: {err_msg}", "error")
                _add_log(task_id, f"Process exited with code {proc.returncode}", "error")

            # Set to review instead of completed — user decides when it's truly done
            if current and current["status"] == "running":
                new_status = "review" if proc.returncode == 0 else "failed"
                conn.execute(
                    "UPDATE tasks SET status = ?, completed_at = datetime('now') WHERE id = ?",
                    (new_status, task_id),
                )
                _add_log(task_id, f"Task ready for review" if new_status == "review" else f"Task {new_status}")

            conn.commit()
            conn.close()

        except Exception as e:
            _add_log(task_id, f"Error: {str(e)}", "error")
            conn = get_db()
            current = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current and current["status"] == "running":
                conn.execute(
                    "UPDATE tasks SET status = 'failed', completed_at = datetime('now') WHERE id = ?",
                    (task_id,),
                )
                conn.commit()
            conn.close()

        finally:
            _running_processes.pop(task_id, None)

    thread = threading.Thread(target=_run, daemon=True)
    _running_processes[task_id] = {"thread": thread, "process": None}
    thread.start()


def stop_task(task_id):
    """Stop a running task by killing the Claude CLI process."""
    entry = _running_processes.get(task_id)
    if not entry:
        return False

    proc = entry.get("process")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass

    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', completed_at = datetime('now') WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    conn.close()

    _add_log(task_id, "Task stopped by user")
    return True


def process_queue():
    """Check for queued and unpaused tasks and run them if under concurrency limit."""
    max_concurrent = int(get_setting("max_concurrent_tasks", "1"))
    running_count = len(_running_processes)

    if running_count >= max_concurrent:
        return

    # Unpause tasks whose pause window has expired — move them back to queued
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status = 'queued', paused_until = NULL WHERE status = 'paused' AND paused_until <= datetime('now')"
    )
    conn.commit()

    tasks = conn.execute(
        """SELECT id, pending_follow_up FROM tasks
           WHERE status = 'queued'
           AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
           ORDER BY sort_order ASC, priority DESC, created_at ASC
           LIMIT ?""",
        (max_concurrent - running_count,),
    ).fetchall()
    conn.close()

    for task in tasks:
        follow_up = task["pending_follow_up"]
        run_task(task["id"], follow_up_message=follow_up if follow_up else None)


def scheduler_loop():
    while not _stop_event.is_set():
        try:
            enabled = get_setting("schedule_enabled", "false")
            if enabled == "true":
                process_queue()
        except Exception as e:
            print(f"Scheduler error: {e}")

        _stop_event.wait(10)


def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    _scheduler_thread.start()


def stop_scheduler():
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=5)
