#!/usr/bin/env python3
"""
Telegram Bot Listener — Parking Permit
Commands:
  /dopermit  → triggers permit creation immediately
  /pics      → sends the latest permit screenshots
  /status    → shows last run info from the log
  /start     → shows help
"""

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ── Paths ────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
LOG_FILE = BASE / "bot.log"

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Telegram API helpers ──────────────────────────────────────────────────────

def api(token: str, method: str, **kwargs) -> dict:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            timeout=35,
            **kwargs,
        )
        return r.json()
    except Exception as e:
        log.warning("API call %s failed: %s", method, e)
        return {}


def send(token: str, chat_id: str, text: str):
    api(token, "sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def send_photo(token: str, chat_id: str, path: Path, caption: str = ""):
    with open(path, "rb") as f:
        api(token, "sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f})


def send_typing(token: str, chat_id: str):
    api(token, "sendChatAction",
        data={"chat_id": chat_id, "action": "typing"})


# ── Command handlers ──────────────────────────────────────────────────────────

def handle_do_permit(token: str, chat_id: str):
    """Run permit.py and report result."""
    send(token, chat_id,
         "⏳ <b>Starting permit creation…</b>\nThis takes about 20–60 seconds.")
    send_typing(token, chat_id)

    try:
        result = subprocess.run(
            [sys.executable, str(BASE / "permit.py")],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            send(token, chat_id,
                 "✅ <b>Done!</b> Permit created — check the message above for details.")
        else:
            output = result.stdout[-800:] or result.stderr[-800:] or "No output"
            send(token, chat_id,
                 f"❌ <b>Permit creation failed.</b>\n\n<pre>{output}</pre>")
    except subprocess.TimeoutExpired:
        send(token, chat_id, "⚠️ Permit script timed out after 3 minutes.")
    except Exception as e:
        send(token, chat_id, f"⚠️ Error running permit script: {e}")


def handle_pics(token: str, chat_id: str):
    """Send the latest permit screenshots."""
    groups = {
        "result":   sorted(BASE.glob("result_*.png")),
        "review":   sorted(BASE.glob("review_*.png")),
        "pay2park": sorted(BASE.glob("pay2park_*.png")),
    }

    sent = 0
    for label, files in groups.items():
        if files:
            latest = files[-1]
            ts = latest.stem.split("_", 1)[-1].replace("_", " ")
            captions = {
                "result":   f"📋 <b>Confirmation Page</b>\n{ts}",
                "review":   f"✅ <b>Review &amp; Verify</b>\n{ts}",
                "pay2park": f"💳 <b>Pay 2 Park</b>\n{ts}",
            }
            send_photo(token, chat_id, latest, captions[label])
            sent += 1
            time.sleep(0.5)

    if sent == 0:
        send(token, chat_id,
             "📂 No screenshots found yet. Run /dopermit first.")
    else:
        send(token, chat_id,
             f"📸 Sent {sent} screenshot(s) from the last permit run.")


def handle_status(token: str, chat_id: str):
    """Show last few lines of the permit log."""
    log_file = BASE / "permit.log"
    if not log_file.exists():
        send(token, chat_id, "📄 No log file found yet.")
        return

    lines = log_file.read_text().strip().splitlines()
    last_lines = "\n".join(lines[-15:])
    send(token, chat_id,
         f"📄 <b>Last permit log:</b>\n\n<pre>{last_lines}</pre>")


def handle_start(token: str, chat_id: str):
    send(token, chat_id,
         "👋 <b>Parking Permit Bot</b>\n\n"
         "Available commands:\n"
         "🔹 /dopermit — Create a parking permit now\n"
         "🔹 /pics — Get screenshots from the last permit\n"
         "🔹 /status — Show recent log output\n\n"
         "The permit also runs automatically every 47 hours.")


# ── Polling loop ──────────────────────────────────────────────────────────────

def run():
    cfg = load_config()
    token = cfg["telegram_token"]
    chat_id = cfg["telegram_chat_id"]
    offset = 0

    log.info("Bot started — polling for commands …")
    send(token, chat_id,
         "🤖 <b>Parking Permit Bot is online!</b>\n"
         "Type /dopermit, /pics, or /status.")

    while True:
        try:
            resp = api(token, "getUpdates",
                       data={"offset": offset, "timeout": 30,
                             "allowed_updates": ["message"]})

            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                from_id = str(msg.get("chat", {}).get("id", ""))

                # Reload config each message so new IDs take effect without restart
                cfg = load_config()
                allowed = cfg.get("allowed_chat_ids", [chat_id])
                if from_id not in [str(x) for x in allowed]:
                    log.warning("Ignored message from unknown chat: %s", from_id)
                    continue

                log.info("Command received: %s from %s", text, from_id)

                if text in ("/start", "/help"):
                    handle_start(token, from_id)
                elif text in ("/do-permit", "/dopermit"):
                    handle_do_permit(token, from_id)
                elif text == "/pics":
                    handle_pics(token, from_id)
                elif text == "/status":
                    handle_status(token, from_id)
                else:
                    send(token, from_id,
                         "❓ Unknown command.\n"
                         "Try /dopermit, /pics, or /status.")

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run()
