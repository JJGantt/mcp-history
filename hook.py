#!/usr/bin/env python3
"""
Claude Code Stop hook — fires after EVERY response within a session.

Logs each exchange (user prompt + assistant response) to the local history
directory and POSTs it to the peer node for real-time sync.

Source is determined by: CLAUDE_SOURCE env var > config.default_source.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from history_io import append_entry


def get_peer_url(path: str) -> str | None:
    """Try to reach the peer node's receiver. Returns URL or None."""
    peer = CONFIG.get("peer", {})
    hosts = [h for h in [peer.get("local_ip"), peer.get("tailscale_ip")] if h]
    port = peer.get("receiver_port", 8766)
    for host in hosts:
        try:
            req = urllib.request.Request(f"http://{host}:{port}/status")
            urllib.request.urlopen(req, timeout=5)
            return f"http://{host}:{port}{path}"
        except Exception:
            continue
    return None


def extract_user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
        return " ".join(parts).strip()
    return str(content)


def get_last_user_message(transcript_path: str) -> str:
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return ""
    last_user = ""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "user":
                content = entry.get("message", {}).get("content", "")
                text = extract_user_text(content)
                if text:
                    last_user = text
    except Exception:
        pass
    return last_user


def _notify(message: str):
    try:
        from notify import send_telegram
        send_telegram(message)
    except Exception:
        pass


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    last_assistant = payload.get("last_assistant_message", "").strip()

    if not last_assistant:
        sys.exit(0)

    user_msg = get_last_user_message(transcript_path)
    if not user_msg:
        sys.exit(0)

    source = os.environ.get("CLAUDE_SOURCE", CONFIG["default_source"])

    append_entry(source, user_msg, last_assistant)

    log_url = get_peer_url("/log")
    if not log_url:
        _notify("Warning: history hook could not reach peer — logged locally only.")
        sys.exit(0)

    body = json.dumps({
        "user": user_msg,
        "claude": last_assistant,
        "source": source,
        "timestamp": datetime.now().isoformat(),
    }).encode()

    try:
        req = urllib.request.Request(
            log_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        _notify(f"Warning: history hook failed to POST to peer: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
