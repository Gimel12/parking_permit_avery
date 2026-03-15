#!/usr/bin/env python3
"""
Parking Permit — Health Watchdog
Runs every hour via launchd. Checks:
  1. Flask web app is responding on localhost:5055
  2. Cloudflare tunnel is reachable (parking.mrfinancebizz.com)
  3. Permit is not expired / overdue
Sends Telegram alerts for any failures and attempts auto-recovery.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "watchdog.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

PERMIT_THRESHOLD_HOURS = 49  # Alert if permit older than this
APP_URL = "http://localhost:5055/api/status"
TUNNEL_URL = "https://parking.mrfinancebizz.com/api/status"


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def tg_send(token: str, chat_ids: list, text: str):
    """Send Telegram message to one or more chat IDs."""
    for cid in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
        except Exception as e:
            log.warning("Telegram send to %s failed: %s", cid, e)


def restart_service(label: str):
    """Attempt to restart a launchd service."""
    try:
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        subprocess.run(["launchctl", "unload", plist], capture_output=True, timeout=10)
        subprocess.run(["launchctl", "load", plist], capture_output=True, timeout=10)
        log.info("Restarted service: %s", label)
        return True
    except Exception as e:
        log.error("Failed to restart %s: %s", label, e)
        return False


def check_flask_app() -> tuple[bool, str]:
    """Check if the Flask app is responding locally."""
    cfg = load_config()
    user = cfg.get("app_username", "admin")
    pw = cfg.get("app_password", "parking")
    try:
        r = requests.get(APP_URL, auth=(user, pw), timeout=10)
        if r.status_code == 200:
            log.info("✅ Flask app is healthy (HTTP 200)")
            return True, ""
        else:
            msg = f"Flask app returned HTTP {r.status_code}"
            log.error("❌ %s", msg)
            return False, msg
    except requests.ConnectionError:
        msg = "Flask app is DOWN — connection refused on port 5055"
        log.error("❌ %s", msg)
        return False, msg
    except Exception as e:
        msg = f"Flask app check failed: {e}"
        log.error("❌ %s", msg)
        return False, msg


def check_tunnel() -> tuple[bool, str]:
    """Check if the Cloudflare tunnel is reachable."""
    cfg = load_config()
    user = cfg.get("app_username", "admin")
    pw = cfg.get("app_password", "parking")
    try:
        r = requests.get(TUNNEL_URL, auth=(user, pw), timeout=15)
        if r.status_code == 200:
            log.info("✅ Cloudflare tunnel is healthy (HTTP 200)")
            return True, ""
        else:
            msg = f"Tunnel returned HTTP {r.status_code}"
            log.error("❌ %s", msg)
            return False, msg
    except Exception as e:
        msg = f"Tunnel unreachable: {e}"
        log.error("❌ %s", msg)
        return False, msg


def check_permit() -> tuple[bool, str]:
    """Check if the permit is current (not overdue)."""
    if not STATE_FILE.exists():
        msg = "No state.json — permit has never been created"
        log.warning("⚠️ %s", msg)
        return False, msg

    with open(STATE_FILE) as f:
        state = json.load(f)

    last_str = state.get("last_success")
    if not last_str:
        msg = "state.json exists but no last_success recorded"
        log.warning("⚠️ %s", msg)
        return False, msg

    last = datetime.fromisoformat(last_str)
    hours = (datetime.now() - last).total_seconds() / 3600
    confirmation = state.get("confirmation", "N/A")
    expires = state.get("expires", "N/A")

    log.info("Permit: %s — %.1fh ago — expires %s", confirmation, hours, expires)

    if hours < PERMIT_THRESHOLD_HOURS:
        log.info("✅ Permit is current")
        return True, ""
    else:
        msg = (f"Permit OVERDUE — last success {hours:.1f}h ago\n"
               f"Code: {confirmation}\nExpiry: {expires}")
        log.warning("🚨 %s", msg)
        return False, msg


def main():
    cfg = load_config()
    token = cfg["telegram_token"]
    # Support single or multiple chat IDs
    cid = cfg.get("telegram_chat_id", "")
    chat_ids = [c.strip() for c in str(cid).split(",") if c.strip()]

    now = datetime.now()
    log.info("=" * 50)
    log.info("Watchdog check — %s", now.strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 50)

    issues = []

    # ── Check 1: Flask app ────────────────────────────────────────────────────
    app_ok, app_msg = check_flask_app()
    if not app_ok:
        issues.append(("🖥 Web App", app_msg))
        log.info("Attempting auto-recovery: restarting Flask app...")
        restart_service("com.ruben.parking-app")

    # ── Check 2: Cloudflare tunnel ────────────────────────────────────────────
    tunnel_ok, tunnel_msg = check_tunnel()
    if not tunnel_ok:
        issues.append(("🌐 Tunnel", tunnel_msg))
        log.info("Attempting auto-recovery: restarting cloudflared...")
        restart_service("com.ruben.cloudflared")

    # ── Check 3: Permit freshness ─────────────────────────────────────────────
    permit_ok, permit_msg = check_permit()
    if not permit_ok:
        issues.append(("🅿️ Permit", permit_msg))

    # ── Report ────────────────────────────────────────────────────────────────
    if not issues:
        log.info("✅ All checks passed. System healthy.")
        return

    # Build alert message
    alert = "🚨 <b>Parking Permit — Watchdog Alert</b>\n\n"
    for label, msg in issues:
        alert += f"{label}: {msg}\n\n"
    alert += f"🕐 Checked at {now.strftime('%Y-%m-%d %I:%M %p')}\n"
    alert += "👉 Open <a href='https://parking.mrfinancebizz.com'>parking.mrfinancebizz.com</a> to check."

    tg_send(token, chat_ids, alert)
    log.warning("Alert sent for %d issue(s)", len(issues))


if __name__ == "__main__":
    main()
