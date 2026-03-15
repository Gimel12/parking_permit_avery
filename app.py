#!/usr/bin/env python3
"""
Parking Permit — Flask Web App
Dashboard to configure, schedule, and monitor permit automation.
Telegram used for outbound notifications only (no polling).
"""

import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

# ── Paths ────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
SCHEDULE_FILE = BASE / "schedule.json"
APP_LOG_FILE = BASE / "app.log"

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(APP_LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Flask + Scheduler ────────────────────────────────────────────────────────
app = Flask(__name__)
scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

# Lock to prevent overlapping permit runs
permit_lock = threading.Lock()


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_schedule() -> dict:
    """Load schedule config. Defaults to every 2 days at 6:00 AM, disabled."""
    defaults = {"enabled": False, "every_days": 2, "hour": 6, "minute": 0}
    data = load_json(SCHEDULE_FILE)
    return {**defaults, **data}


def save_schedule(data: dict):
    save_json(SCHEDULE_FILE, data)


def get_state() -> dict:
    return load_json(STATE_FILE)


def get_log_tail(filename: str, lines: int = 30) -> str:
    path = BASE / filename
    if not path.exists():
        return "(no log file yet)"
    all_lines = path.read_text().strip().splitlines()
    return "\n".join(all_lines[-lines:])


def run_permit_job(source: str = "scheduled"):
    """Run the permit automation. Thread-safe."""
    if not permit_lock.acquire(blocking=False):
        log.warning("Permit job already running — skipping (%s)", source)
        return

    try:
        log.info("=== Permit job started (%s) ===", source)
        from permit import main as permit_main
        success, result = permit_main()
        if success:
            log.info("Permit job succeeded: %s", result.get("confirmation", "N/A"))
        else:
            log.error("Permit job failed after 3 attempts")
    except Exception as e:
        log.error("Permit job exception: %s", e, exc_info=True)
    finally:
        permit_lock.release()


def sync_scheduler():
    """Apply the current schedule config to APScheduler."""
    job_id = "permit_job"

    # Remove existing job if any
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    sched = load_schedule()
    if sched.get("enabled"):
        every = max(1, sched.get("every_days", 2))
        if every == 1:
            # Daily: use cron trigger
            trigger = CronTrigger(hour=sched["hour"], minute=sched["minute"])
        else:
            # Every N days: use interval trigger, aligned to the chosen time
            from datetime import timedelta
            now = datetime.now()
            start = now.replace(hour=sched["hour"], minute=sched["minute"], second=0, microsecond=0)
            if start <= now:
                start += timedelta(days=1)
            trigger = IntervalTrigger(days=every, start_date=start)
        scheduler.add_job(
            run_permit_job,
            trigger=trigger,
            id=job_id,
            kwargs={"source": "scheduled"},
            replace_existing=True,
        )
        log.info("Scheduler set: every %d day(s) at %02d:%02d", every, sched["hour"], sched["minute"])
    else:
        log.info("Scheduler disabled.")


# ── Initialize scheduler on startup ─────────────────────────────────────────
sync_scheduler()


# ── HTML Template ────────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parking Permit</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --card2: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #3b82f6;
    --green: #22c55e; --red: #ef4444; --yellow: #eab308;
    --border: #475569; --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 800px; margin: 0 auto; }
  h1 { font-size: 1.8rem; margin-bottom: 6px; }
  .subtitle { color: var(--muted); margin-bottom: 24px; font-size: 0.95rem; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  @media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--card); border-radius: var(--radius);
    padding: 20px; border: 1px solid var(--border);
  }
  .card h2 { font-size: 1rem; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .card .value { font-size: 1.4rem; font-weight: 700; }
  .card .detail { color: var(--muted); font-size: 0.85rem; margin-top: 4px; }

  .status-ok { color: var(--green); }
  .status-warn { color: var(--yellow); }
  .status-bad { color: var(--red); }

  .full { grid-column: 1 / -1; }

  /* Form elements */
  label { display: block; color: var(--muted); font-size: 0.85rem; margin-bottom: 4px; font-weight: 500; }
  input[type="text"], input[type="password"], input[type="number"], input[type="email"] {
    width: 100%; padding: 10px 12px; background: var(--card2); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-size: 0.95rem; margin-bottom: 12px;
    outline: none; transition: border-color 0.2s;
  }
  input:focus { border-color: var(--accent); }

  .row { display: flex; gap: 12px; align-items: end; }
  .row > div { flex: 1; }

  select {
    width: 100%; padding: 10px 12px; background: var(--card2); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-size: 0.95rem; margin-bottom: 12px;
  }

  .toggle-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .toggle {
    position: relative; width: 48px; height: 26px; cursor: pointer;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle .slider {
    position: absolute; inset: 0; background: var(--card2); border-radius: 13px;
    border: 1px solid var(--border); transition: background 0.3s;
  }
  .toggle .slider::before {
    content: ''; position: absolute; width: 20px; height: 20px; left: 2px; top: 2px;
    background: var(--muted); border-radius: 50%; transition: transform 0.3s, background 0.3s;
  }
  .toggle input:checked + .slider { background: var(--accent); border-color: var(--accent); }
  .toggle input:checked + .slider::before { transform: translateX(22px); background: white; }

  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 10px 20px; border: none; border-radius: 8px;
    font-size: 0.95rem; font-weight: 600; cursor: pointer; transition: all 0.2s;
    text-decoration: none;
  }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { background: #2563eb; }
  .btn-green { background: var(--green); color: white; }
  .btn-green:hover { background: #16a34a; }
  .btn-red { background: var(--red); color: white; }
  .btn-red:hover { background: #dc2626; }
  .btn-outline { background: transparent; color: var(--text); border: 1px solid var(--border); }
  .btn-outline:hover { background: var(--card2); }
  .btn-sm { padding: 6px 14px; font-size: 0.85rem; }
  .btn-group { display: flex; gap: 8px; flex-wrap: wrap; }

  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px; font-size: 0.78rem; overflow-x: auto; line-height: 1.5;
    color: var(--muted); max-height: 350px; overflow-y: auto;
  }

  .screenshots { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  .screenshots img {
    max-width: 180px; border-radius: 8px; border: 1px solid var(--border);
    cursor: pointer; transition: transform 0.2s;
  }
  .screenshots img:hover { transform: scale(1.05); }

  .flash {
    padding: 12px 16px; border-radius: 8px; margin-bottom: 16px;
    font-weight: 500; font-size: 0.9rem;
  }
  .flash-success { background: #16a34a22; border: 1px solid var(--green); color: var(--green); }
  .flash-error { background: #dc262622; border: 1px solid var(--red); color: var(--red); }
  .flash-info { background: #3b82f622; border: 1px solid var(--accent); color: var(--accent); }

  .spinner {
    display: inline-block; width: 18px; height: 18px;
    border: 2px solid rgba(255,255,255,0.3); border-top-color: white;
    border-radius: 50%; animation: spin 0.6s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  footer { text-align: center; color: var(--muted); font-size: 0.8rem; margin-top: 30px; padding: 16px 0; }
</style>
</head>
<body>
<div class="container">

<h1>🅿️ Parking Permit</h1>
<p class="subtitle">Automated 48h visitor permit for TESLA MODEL 3</p>

{% if flash %}
<div class="flash flash-{{ flash.type }}">{{ flash.msg }}</div>
{% endif %}

<!-- Status Cards -->
<div class="grid">
  <div class="card">
    <h2>Permit Status</h2>
    {% if state.last_success %}
      {% if hours_since < 48 %}
        <div class="value status-ok">✅ Active</div>
      {% elif hours_since < 50 %}
        <div class="value status-warn">⚠️ Expiring Soon</div>
      {% else %}
        <div class="value status-bad">❌ Expired</div>
      {% endif %}
      <div class="detail">{{ state.confirmation }}</div>
      <div class="detail">Expires: {{ state.expires }}</div>
    {% else %}
      <div class="value status-bad">❌ No Record</div>
      <div class="detail">No permit has been created yet</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Schedule</h2>
    {% if schedule.enabled %}
      {% if schedule.every_days == 1 %}
        <div class="value status-ok">⏰ Daily at {{ "%02d"|format(schedule.hour) }}:{{ "%02d"|format(schedule.minute) }}</div>
      {% else %}
        <div class="value status-ok">⏰ Every {{ schedule.every_days }} days at {{ "%02d"|format(schedule.hour) }}:{{ "%02d"|format(schedule.minute) }}</div>
      {% endif %}
      {% if next_run %}
        <div class="detail">Next run: {{ next_run }}</div>
      {% endif %}
    {% else %}
      <div class="value status-warn">⏸ Disabled</div>
      <div class="detail">No automatic runs scheduled</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Last Run</h2>
    {% if state.last_success %}
      <div class="value">{{ hours_since|round(1) }}h ago</div>
      <div class="detail">{{ state.last_success[:19] | replace('T', ' ') }}</div>
    {% else %}
      <div class="value">—</div>
      <div class="detail">Never</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Quick Actions</h2>
    <div class="btn-group" style="margin-top:4px">
      <form method="POST" action="/run" id="runForm">
        <button type="submit" class="btn btn-green btn-sm" id="runBtn" onclick="this.innerHTML='<span class=spinner></span> Running…'; this.disabled=true; document.getElementById('runForm').submit();">
          ▶ Run Now
        </button>
      </form>
      <a href="/" class="btn btn-outline btn-sm">↻ Refresh</a>
    </div>
  </div>
</div>

<!-- Schedule Config -->
<div class="card" style="margin-bottom:16px">
  <h2>Schedule Configuration</h2>
  <form method="POST" action="/schedule">
    <div class="toggle-row">
      <label class="toggle">
        <input type="checkbox" name="enabled" value="1" {{ 'checked' if schedule.enabled }}>
        <span class="slider"></span>
      </label>
      <span>Enable automatic schedule</span>
    </div>
    <div class="row">
      <div>
        <label>Run every N days</label>
        <select name="every_days">
          <option value="1" {{ 'selected' if schedule.every_days == 1 }}>Every day</option>
          <option value="2" {{ 'selected' if schedule.every_days == 2 }}>Every 2 days</option>
          <option value="3" {{ 'selected' if schedule.every_days == 3 }}>Every 3 days</option>
          <option value="4" {{ 'selected' if schedule.every_days == 4 }}>Every 4 days</option>
          <option value="5" {{ 'selected' if schedule.every_days == 5 }}>Every 5 days</option>
          <option value="7" {{ 'selected' if schedule.every_days == 7 }}>Every 7 days</option>
        </select>
      </div>
      <div>
        <label>Hour (0–23)</label>
        <input type="number" name="hour" min="0" max="23" value="{{ schedule.hour }}">
      </div>
      <div>
        <label>Minute (0–59)</label>
        <input type="number" name="minute" min="0" max="59" value="{{ schedule.minute }}">
      </div>
    </div>
    <div style="margin-top:4px">
      <button type="submit" class="btn btn-primary btn-sm">Save Schedule</button>
    </div>
  </form>
</div>

<!-- Credentials Config -->
<div class="card" style="margin-bottom:16px">
  <h2>Credentials</h2>
  <form method="POST" action="/config">
    <div class="row">
      <div>
        <label>Username / Email</label>
        <input type="email" name="username" value="{{ config.username }}" autocomplete="off">
      </div>
      <div>
        <label>Password</label>
        <input type="password" name="password" value="{{ config.password }}" autocomplete="off">
      </div>
    </div>
    <div class="row">
      <div>
        <label>Telegram Bot Token</label>
        <input type="text" name="telegram_token" value="{{ config.telegram_token }}" autocomplete="off">
      </div>
      <div>
        <label>Telegram Chat ID</label>
        <input type="text" name="telegram_chat_id" value="{{ config.telegram_chat_id }}">
      </div>
    </div>
    <button type="submit" class="btn btn-primary btn-sm">Save Credentials</button>
  </form>
</div>

<!-- Screenshots -->
<div class="card" style="margin-bottom:16px">
  <h2>Latest Screenshots</h2>
  <div class="screenshots">
    {% for img in screenshots %}
      <a href="/screenshot/{{ img.name }}" target="_blank">
        <img src="/screenshot/{{ img.name }}" alt="{{ img.name }}" title="{{ img.name }}">
      </a>
    {% else %}
      <span style="color:var(--muted)">No screenshots yet. Run the permit to generate them.</span>
    {% endfor %}
  </div>
</div>

<!-- Logs -->
<div class="card">
  <h2>Recent Logs</h2>
  <div class="btn-group" style="margin-bottom:10px">
    <a href="/?log=permit" class="btn btn-outline btn-sm {{ 'btn-primary' if active_log == 'permit' }}">permit.log</a>
    <a href="/?log=app" class="btn btn-outline btn-sm {{ 'btn-primary' if active_log == 'app' }}">app.log</a>
    <a href="/?log=watchdog" class="btn btn-outline btn-sm {{ 'btn-primary' if active_log == 'watchdog' }}">watchdog.log</a>
  </div>
  <pre>{{ log_content }}</pre>
</div>

<footer>Parking Permit Automation &middot; Flask + APScheduler</footer>
</div>
</body>
</html>
"""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    config = load_json(CONFIG_FILE)
    state = get_state()
    schedule = load_schedule()

    # Calculate hours since last success
    hours_since = 999
    if state.get("last_success"):
        try:
            last = datetime.fromisoformat(state["last_success"])
            hours_since = (datetime.now() - last).total_seconds() / 3600
        except Exception:
            pass

    # Next run time
    next_run = None
    job = scheduler.get_job("permit_job")
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M")

    # Log viewer
    active_log = request.args.get("log", "permit")
    log_map = {"permit": "permit.log", "app": "app.log", "watchdog": "watchdog.log"}
    log_content = get_log_tail(log_map.get(active_log, "permit.log"), lines=40)

    # Latest screenshots (last 6)
    pngs = sorted(BASE.glob("result_*.png"), key=lambda p: p.stat().st_mtime)
    screenshots = pngs[-6:]

    # Flash messages via query param
    flash = None
    fm = request.args.get("flash")
    if fm == "run_started":
        flash = {"type": "info", "msg": "⏳ Permit job started in the background. Refresh in ~60s to see results."}
    elif fm == "schedule_saved":
        flash = {"type": "success", "msg": "✅ Schedule saved successfully."}
    elif fm == "config_saved":
        flash = {"type": "success", "msg": "✅ Credentials saved."}
    elif fm == "already_running":
        flash = {"type": "error", "msg": "⚠️ A permit job is already running. Please wait."}

    return render_template_string(
        TEMPLATE,
        config=config,
        state=state,
        schedule=schedule,
        hours_since=hours_since,
        next_run=next_run,
        active_log=active_log,
        log_content=log_content,
        screenshots=screenshots,
        flash=flash,
    )


@app.route("/run", methods=["POST"])
def run_now():
    if permit_lock.locked():
        return redirect(url_for("index", flash="already_running"))

    thread = threading.Thread(target=run_permit_job, kwargs={"source": "manual"}, daemon=True)
    thread.start()
    return redirect(url_for("index", flash="run_started"))


@app.route("/schedule", methods=["POST"])
def update_schedule():
    data = {
        "enabled": "enabled" in request.form,
        "every_days": int(request.form.get("every_days", 2)),
        "hour": int(request.form.get("hour", 6)),
        "minute": int(request.form.get("minute", 0)),
    }
    save_schedule(data)
    sync_scheduler()
    return redirect(url_for("index", flash="schedule_saved"))


@app.route("/config", methods=["POST"])
def update_config():
    config = load_json(CONFIG_FILE)
    config["username"] = request.form.get("username", config.get("username", ""))
    config["password"] = request.form.get("password", config.get("password", ""))
    config["telegram_token"] = request.form.get("telegram_token", config.get("telegram_token", ""))
    config["telegram_chat_id"] = request.form.get("telegram_chat_id", config.get("telegram_chat_id", ""))
    save_json(CONFIG_FILE, config)
    return redirect(url_for("index", flash="config_saved"))


@app.route("/screenshot/<filename>")
def screenshot(filename):
    from flask import send_from_directory
    return send_from_directory(str(BASE), filename)


@app.route("/api/status")
def api_status():
    """JSON endpoint for external checks."""
    state = get_state()
    schedule = load_schedule()
    hours_since = 999
    if state.get("last_success"):
        try:
            last = datetime.fromisoformat(state["last_success"])
            hours_since = (datetime.now() - last).total_seconds() / 3600
        except Exception:
            pass

    return jsonify({
        "permit_active": hours_since < 48,
        "hours_since_last": round(hours_since, 1),
        "confirmation": state.get("confirmation"),
        "expires": state.get("expires"),
        "schedule_enabled": schedule.get("enabled"),
        "schedule_every_days": schedule.get("every_days", 2),
        "schedule_time": f"{schedule.get('hour', 0):02d}:{schedule.get('minute', 0):02d}",
        "running": permit_lock.locked(),
    })


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting Parking Permit web app on http://localhost:5055")
    app.run(host="0.0.0.0", port=5055, debug=False)
