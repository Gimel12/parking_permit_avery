#!/usr/bin/env python3
"""
Parking Permit Automation — peosfweb.com
Automatically creates a 48-hour visitor permit for the TESLA profile.
Sends a Telegram notification with confirmation screenshot after each run.
Runs every 47 hours via launchd (1-hour overlap ensures no permit gap).
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Paths ────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
LOG_FILE = BASE / "permit.log"

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

# ── Constants ────────────────────────────────────────────────────────────────
LOGIN_URL = "https://www.peosfweb.com/account/login"
TESLA_PROFILE_LABEL = "TESLA"
TESLA_PROFILE_VALUE = "TV-F4069C01-81FF-8778-98D6-1B04144590BE"
VEHICLE_STATE = "FL"
BROWSER_ARGS = [
    "--ignore-certificate-errors",
    "--ignore-certificate-errors-spki-list",
    "--ignore-urlfetcher-cert-requests",
    "--disable-features=HttpsUpgrades,HttpsFirstModeIncognito,HttpsFirstMode",
    "--allow-running-insecure-content",
    "--disable-web-security",
    "--ssl-version-min=tls1",
]


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.json not found.")
        raise FileNotFoundError("config.json not found")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def snap(page, label: str) -> Path:
    """Save a screenshot and return its path."""
    path = BASE / f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        page.screenshot(path=str(path))
    except Exception:
        pass
    return path


# ── Telegram helpers ─────────────────────────────────────────────────────────

def tg_send(token: str, chat_id: str, text: str):
    """Send a text message via Telegram."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        log.warning("Telegram message failed: %s", e)


def tg_photo(token: str, chat_id: str, photo_path: Path, caption: str = ""):
    """Send a photo via Telegram."""
    try:
        with open(photo_path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=30,
            )
    except Exception as e:
        log.warning("Telegram photo failed: %s", e)


def notify_success(cfg: dict, confirmation: str, expires: str, result_screenshot: Path):
    """Send a success notification with the result screenshot."""
    token = cfg.get("telegram_token")
    chat_id = cfg.get("telegram_chat_id")
    if not token or not chat_id:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    caption = (
        f"✅ <b>Parking Permit Created!</b>\n\n"
        f"🚗 <b>Vehicle:</b> TESLA MODEL 3 WHITE (RBTU67)\n"
        f"🏷 <b>Confirmation:</b> <code>{confirmation}</code>\n"
        f"⏰ <b>Expires:</b> {expires}\n"
        f"🕐 <b>Run at:</b> {now}\n\n"
        f"Next renewal in ~47 hours."
    )
    tg_photo(token, chat_id, result_screenshot, caption)
    log.info("Telegram notification sent.")


def notify_failure(cfg: dict, reason: str, error_screenshot: Path = None):
    """Send a failure alert."""
    token = cfg.get("telegram_token")
    chat_id = cfg.get("telegram_chat_id")
    if not token or not chat_id:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = (
        f"❌ <b>Parking Permit FAILED!</b>\n\n"
        f"Reason: {reason}\n"
        f"Time: {now}\n\n"
        f"All 3 attempts failed. Please create the permit manually."
    )
    tg_send(token, chat_id, msg)

    if error_screenshot and error_screenshot.exists():
        tg_photo(token, chat_id, error_screenshot, "Error screenshot")

    log.info("Telegram failure alert sent.")


# ── Main automation ──────────────────────────────────────────────────────────

def create_permit() -> tuple[bool, dict]:
    """Run the full permit flow. Returns (success, result_data)."""
    cfg = load_config()
    result = {"confirmation": "N/A", "expires": "N/A", "screenshot": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        page.on("dialog", lambda d: d.accept())

        try:
            # ── Step 1: Login ────────────────────────────────────────────────
            log.info("Step 1 — Logging in …")
            try:
                page.goto(LOGIN_URL, timeout=20_000)
            except Exception as ssl_err:
                if "SSL" in str(ssl_err) or "ERR_SSL" in str(ssl_err) or "net::" in str(ssl_err):
                    log.warning("HTTPS failed (%s) — retrying with HTTP …", ssl_err)
                    page.goto(LOGIN_URL.replace("https://", "http://"), timeout=20_000)
                else:
                    raise
            page.fill("#MainContent_UserName", cfg["username"])
            page.fill("#MainContent_Password", cfg["password"])
            page.click("#MainContent_Button1")
            page.wait_for_load_state("networkidle")

            if "login" in page.url.lower():
                log.error("Login failed.")
                err = snap(page, "error_login")
                result["screenshot"] = err
                return False, result
            log.info("Logged in.")

            # ── Step 2: Open modal ───────────────────────────────────────────
            log.info("Step 2 — Opening Create Visitor Permit modal …")
            page.click("text=CREATE A VISITOR PERMIT")
            page.wait_for_timeout(2000)

            # ── Step 3: Select TESLA + confirm dialog ────────────────────────
            log.info("Step 3 — Selecting TESLA profile …")
            page.select_option("#MainContent_ddl_Visitors", label=TESLA_PROFILE_LABEL)
            page.wait_for_timeout(500)
            page.click("#MainContent_btn_Assign_Profile")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            if "Visitors" not in page.url:
                log.error("Failed to reach vehicle info page.")
                err = snap(page, "error_step3")
                result["screenshot"] = err
                return False, result
            log.info("Step 3 done.")

            # ── Step 4: Vehicle Information ──────────────────────────────────
            log.info("Step 4 — Selecting TESLA vehicle profile …")
            page.select_option("#MainContent_ddl_Vehicle_Profile", value=TESLA_PROFILE_VALUE)
            page.wait_for_timeout(3000)

            if not page.locator("#MainContent_txt_Plate").input_value():
                log.warning("Auto-fill incomplete — filling manually.")
                page.fill("#MainContent_txt_Plate", "RBTU67")
                page.fill("#MainContent_txt_Make", "TESLA")
                page.fill("#MainContent_txt_Model", "MODEL 3")
                page.fill("#MainContent_txt_Color", "WHITE")

            page.select_option("#MainContent_ddl_State", value=VEHICLE_STATE)
            page.wait_for_timeout(500)
            page.click("#MainContent_btn_Vehicle_Next")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
            log.info("Step 4 done.")

            # ── Step 5: Pay 2 Park — Maximum 48h ────────────────────────────────
            log.info("Step 5 — Pay 2 Park …")
            page.check("#MainContent_rb_Maxtime")
            page.wait_for_timeout(500)
            page.click("#MainContent_btn_Auth_Next")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
            log.info("Step 5 done.")

            # ── Step 6: Review & Verify ──────────────────────────────────────
            log.info("Step 6 — Submitting …")
            if not page.locator("#MainContent_cb_Confirm").is_checked():
                page.check("#MainContent_cb_Confirm")
            page.wait_for_timeout(500)
            page.click("#MainContent_btn_Submit")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(3000)

            # ── Step 7: Parse result ─────────────────────────────────────────
            result_path = snap(page, "result")
            result["screenshot"] = result_path
            content = page.locator("body").inner_text()

            for line in content.splitlines():
                line = line.strip()
                if line.startswith("VC-") or line.startswith("vc-"):
                    result["confirmation"] = line
                if "/" in line and "202" in line and ("AM" in line or "PM" in line):
                    if len(line) < 40:
                        result["expires"] = line

            log.info("✅ Permit created! Confirmation: %s Expires: %s",
                     result["confirmation"], result["expires"])

            # Save state
            state_path = BASE / "state.json"
            with open(state_path, "w") as _f:
                json.dump({
                    "last_success":  datetime.now().isoformat(),
                    "confirmation":  result["confirmation"],
                    "expires":       result["expires"],
                }, _f, indent=2)

            return True, result

        except PlaywrightTimeout as e:
            log.error("Timeout: %s", e)
            result["screenshot"] = snap(page, "error_timeout")
            return False, result
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            result["screenshot"] = snap(page, "error_unexpected")
            return False, result
        finally:
            browser.close()


def main():
    log.info("=" * 60)
    log.info("Parking permit job started — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 60)

    cfg = load_config()
    last_result = {}

    for attempt in range(1, 4):
        log.info("Attempt %d/3", attempt)
        success, last_result = create_permit()

        if success:
            notify_success(
                cfg,
                confirmation=last_result.get("confirmation", "N/A"),
                expires=last_result.get("expires", "N/A"),
                result_screenshot=last_result.get("screenshot"),
            )
            log.info("Job complete.")
            return True, last_result

        if attempt < 3:
            log.warning("Attempt %d failed — retrying in 60s …", attempt)
            time.sleep(60)

    notify_failure(
        cfg,
        reason="All 3 automation attempts failed",
        error_screenshot=last_result.get("screenshot"),
    )
    log.error("All 3 attempts failed.")
    return False, last_result


if __name__ == "__main__":
    main()
