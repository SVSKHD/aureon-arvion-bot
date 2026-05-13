"""Telegram notifier. Failures are logged as warnings only — never crash the bot."""

import logging
from typing import Optional

import requests

import config


class TelegramNotifier:
    """Safe Telegram client. Disabled silently if config is incomplete."""

    def __init__(self, log: Optional[logging.Logger] = None):
        self.log = log or logging.getLogger("anchor_bot")
        self.enabled = bool(
            config.TELEGRAM_ENABLED
            and config.TELEGRAM_BOT_TOKEN
            and config.TELEGRAM_CHAT_ID
        )
        if config.TELEGRAM_ENABLED and not self.enabled:
            self.log.warning("Telegram enabled in config but token/chat_id missing — disabled.")
        elif self.enabled:
            self.log.info("Telegram notifications: ENABLED")
        else:
            self.log.info("Telegram notifications: DISABLED")

    def send_message(self, text: str) -> bool:
        """Send a message. Returns True on success, False otherwise. Never raises."""
        if not self.enabled:
            return False

        # Log the first line of the message at INFO so logs show what was attempted.
        first_line = text.splitlines()[0] if text else ""
        preview = first_line[:60] + ("..." if len(first_line) > 60 else "")

        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code == 200:
                self.log.info(f"Telegram sent: {preview}")
                return True
            # Log the actual response body for diagnosis
            try:
                err_desc = r.json().get("description", r.text[:200])
            except Exception:
                err_desc = r.text[:200]
            self.log.warning(
                f"Telegram FAILED [{r.status_code}]: {err_desc} | msg: {preview}"
            )
            return False
        except requests.RequestException as e:
            self.log.warning(f"Telegram network error: {e} | msg: {preview}")
            return False
        except Exception as e:
            self.log.warning(f"Telegram unexpected error: {e} | msg: {preview}")
            return False
