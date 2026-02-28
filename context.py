#!/usr/bin/env python3
"""
Build a context string from local history for use with claude --append-system-prompt.

Usage:
  context.py [--hours N] [--channel CHANNEL]

Channels:
  mac        claude-mac, codex-mac, claude-pi, codex-pi  (default)
  telegram   all telegram sources
  sonnet-telegram / opus-telegram / haiku-telegram / codex-telegram / pi-telegram
  voice      claude-voice
  all        every recognized source
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import HISTORY_DIR
from meta_filter import is_meta_entry

_SOURCE_TO_CHANNEL = {
    "sonnet-telegram":  "sonnet-telegram",
    "opus-telegram":    "opus-telegram",
    "haiku-telegram":   "haiku-telegram",
    "codex-telegram":   "codex-telegram",
    "pi-telegram":      "pi-telegram",
    "claude-http":      "http",
    "codex-http":       "http",
    "claude-mac":       "mac",
    "codex-mac":        "mac",
    "claude-pi":        "mac",
    "codex-pi":         "mac",
    "claude-voice":     "voice",
    "claude-telegram":  "telegram",
    "telegram":         "telegram",
    "http":             "http",
    "laptop":           "mac",
    "interactive":      "pi",
}

_GAP_THRESHOLD_SECS = 30 * 60

_CONTEXT_FRAMING = """\
The conversation below is recent context from this channel.

Treat this as the active thread:
- If the user references earlier messages, resolve from this context first.
- Do not claim you lack memory when relevant details are present below.
- Use external history tools only when this context is insufficient.

Use it for continuity but do not reference this framing — respond naturally \
as if continuing a normal conversation.

---
Previous conversation (oldest -> newest):\
"""


def _load_day(date: datetime) -> list:
    f = HISTORY_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


def load_history(hours: int, channel: str | None) -> list:
    now = datetime.now()
    start = now - timedelta(hours=hours)

    results = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_day:
        for entry in _load_day(day):
            try:
                ts = datetime.fromisoformat(entry["timestamp"]).replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            if start <= ts <= now:
                results.append(entry)
        day += timedelta(days=1)

    results.sort(key=lambda e: e.get("timestamp", ""))

    cleaned = []
    for entry in results:
        src = entry.get("source", "")
        entry_channel = _SOURCE_TO_CHANNEL.get(src)
        if entry_channel is None:
            continue
        if channel is not None and entry_channel != channel:
            continue
        if is_meta_entry(entry):
            continue
        cleaned.append(entry)

    return cleaned


def _format_gap(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {remaining}m"


def build_context(entries: list, max_chars: int = 12000) -> str | None:
    if not entries:
        return None

    selected_reversed = []
    total = 0
    for entry in reversed(entries):
        ts = datetime.fromisoformat(entry["timestamp"])
        ts_str = ts.strftime("%Y-%m-%d %H:%M")
        user = entry["user"][:450]
        claude = entry["claude"][:450]
        block = f"[{ts_str}] User: {user}\n[{ts_str}] Response: {claude}"
        if total + len(block) > max_chars and selected_reversed:
            break
        selected_reversed.append(entry)
        total += len(block)

    selected = list(reversed(selected_reversed))

    context_lines = []
    prev_ts = None
    for entry in selected:
        ts = datetime.fromisoformat(entry["timestamp"])
        ts_str = ts.strftime("%Y-%m-%d %H:%M")
        user = entry["user"][:450]
        claude = entry["claude"][:450]
        if prev_ts is not None:
            gap_secs = (ts - prev_ts).total_seconds()
            if gap_secs >= _GAP_THRESHOLD_SECS:
                context_lines.append(f"\n--- {_format_gap(int(gap_secs))} later ---\n")
        context_lines.append(f"[{ts_str}] User: {user}\n[{ts_str}] Response: {claude}")
        prev_ts = ts

    parts = [_CONTEXT_FRAMING]
    parts.extend(context_lines)
    parts.append("---")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Build Claude context from history")
    parser.add_argument("--hours", type=int, default=2)
    parser.add_argument("--channel", type=str, default="mac",
                        help="Channel to filter: mac, telegram, sonnet-telegram, "
                             "opus-telegram, haiku-telegram, codex-telegram, "
                             "pi-telegram, voice, all")
    args = parser.parse_args()

    channel = None if args.channel == "all" else args.channel
    entries = load_history(hours=args.hours, channel=channel)
    ctx = build_context(entries)

    if ctx:
        print(ctx, end="")


if __name__ == "__main__":
    main()
