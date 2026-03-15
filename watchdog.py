#!/usr/bin/env python3
"""
Parking Permit Watchdog
Runs every hour via launchd.
If the last successful permit is older than 47 hours → Telegram alert.
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE  = BASE / "state.json"
LOG_FILE    = BASE / "watchdog.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

ALERT_THRESHOLD_HOURS = 47


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def tg_send(token: str, chat_id: str, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        log.warning("Telegram failed: %s", e)


def main():
    cfg = load_config()
    token   = cfg["telegram_token"]
    chat_id = cfg["telegram_chat_id"]
    now     = datetime.now()

    log.info("Watchdog check — %s", now.strftime("%Y-%m-%d %H:%M"))

    # ── No state file = never ran successfully ───────────────────────────────
    if not STATE_FILE.exists():
        log.warning("state.json missing — permit has never run successfully.")
        tg_send(token, chat_id,
            "⚠️ <b>Parking Permit — No Record Found!</b>\n\n"
            "The automation has never completed successfully or state file is missing.\n\n"
            "👉 Please create the permit manually at peosfweb.com\n"
            "or send /dopermit to trigger it now.")
        sys.exit(1)

    # ── Load last run state ──────────────────────────────────────────────────
    with open(STATE_FILE) as f:
        state = json.load(f)

    last_success_str  = state.get("last_success")
    last_confirmation = state.get("confirmation", "N/A")
    last_expires      = state.get("expires", "N/A")

    if not last_success_str:
        log.warning("state.json exists but no last_success timestamp.")
        tg_send(token, chat_id,
            "⚠️ <b>Parking Permit Watchdog Alert</b>\n\n"
            "state.json found but no successful run recorded.\n"
            "👉 Send /dopermit or create the permit manually.")
        sys.exit(1)

    last_success = datetime.fromisoformat(last_success_str)
    hours_since  = (now - last_success).total_seconds() / 3600

    log.info("Last success: %s (%.1fh ago) — Confirmation: %s",
             last_success_str, hours_since, last_confirmation)

    # ── All good ─────────────────────────────────────────────────────────────
    if hours_since < ALERT_THRESHOLD_HOURS:
        log.info("✅ Permit is current. No action needed.")
        sys.exit(0)

    # ── OVERDUE — send alert ─────────────────────────────────────────────────
    log.warning("⚠️ Permit overdue! Last success was %.1fh ago.", hours_since)
    tg_send(token, chat_id,
        f"🚨 <b>Parking Permit OVERDUE!</b>\n\n"
        f"Last successful permit was <b>{hours_since:.1f} hours ago</b>.\n"
        f"🏷 Last code: <code>{last_confirmation}</code>\n"
        f"⏰ Last expiry: {last_expires}\n\n"
        f"The automation may have failed or not run.\n\n"
        f"👉 Send /dopermit to retry now, or create it manually at peosfweb.com")
    sys.exit(1)


if __name__ == "__main__":
    main()
