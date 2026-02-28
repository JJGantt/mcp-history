#!/usr/bin/env python3
"""
Telegram notification helper. Reads credentials from config.json.

Usage:
  python3 notify.py "message text"

Or import:
  from notify import send_telegram, notify
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG

_NOTIF = CONFIG.get("notifications", {})
_BOT_TOKEN = _NOTIF.get("telegram_bot_token", "")
_CHAT_ID = _NOTIF.get("telegram_chat_id", 0)
_ENABLED = _NOTIF.get("enabled", False) and _BOT_TOKEN and _CHAT_ID


def send_telegram(message: str, bot_token: str = "", chat_id: int = 0) -> bool:
    token = bot_token or _BOT_TOKEN
    cid = chat_id or _CHAT_ID
    if not token or not cid:
        return False
    try:
        body = json.dumps({"chat_id": cid, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def log_to_history(message: str):
    """Log notification to peer's history via POST /log."""
    peer = CONFIG.get("peer", {})
    hosts = [h for h in [peer.get("local_ip"), peer.get("tailscale_ip")] if h]
    port = peer.get("receiver_port", 8766)
    for host in hosts:
        try:
            body = json.dumps({
                "user": "[system notification]",
                "claude": message,
                "source": "system",
                "timestamp": datetime.now().isoformat(),
            }).encode()
            req = urllib.request.Request(
                f"http://{host}:{port}/log",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            return
        except Exception:
            continue


def notify(message: str):
    """Send Telegram notification and log to history."""
    if _ENABLED:
        send_telegram(message)
    log_to_history(message)


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Notification (no message)"
    notify(msg)
