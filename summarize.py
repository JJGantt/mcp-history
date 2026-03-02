#!/usr/bin/env python3
"""
Session summarizer — runs on a schedule (every 5 minutes via launchd).

For each session group (defined by 30-min gap boundaries), re-summarizes if
new entries have appeared since the last summary. This means:
  - Active sessions get a rolling update as new exchanges come in.
  - Completed sessions get finalized once and are only touched if new
    entries somehow appear (e.g. delayed sync from another device).

Run by launchd — no forking needed, no stop hook recursion possible.
"""

import json
import os
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG, SUMMARIES_DIR, get_channel
from history_io import load_history_range, write_json_atomic
from meta_filter import is_meta_entry

_SUMMARIZER = CONFIG.get("summarizer", {})
GAP_THRESHOLD_SECS = _SUMMARIZER.get("gap_threshold_secs", 1800)
LOOKBACK_HOURS = _SUMMARIZER.get("lookback_hours", 48)


def load_summaries_by_start() -> dict:
    """Load all existing summaries keyed by session start timestamp."""
    summaries = {}
    for f in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            for s in json.loads(f.read_text()):
                key = s.get("start", "")
                if key:
                    summaries[key] = s
        except Exception:
            pass
    return summaries


def save_or_update_summary(summary: dict) -> None:
    """Write or update a single summary in its date file, matched by start."""
    date_str = summary["date"]
    summary_file = SUMMARIES_DIR / f"{date_str}.json"

    existing = []
    if summary_file.exists():
        try:
            existing = json.loads(summary_file.read_text())
        except Exception:
            pass

    start = summary.get("start", "")
    replaced = False
    for i, s in enumerate(existing):
        if s.get("start") == start:
            existing[i] = summary
            replaced = True
            break
    if not replaced:
        existing.append(summary)

    write_json_atomic(summary_file, existing)


def group_into_sessions(entries: list) -> list[tuple[str, list]]:
    """Group entries into (channel, session_entries) pairs by 30-min gaps."""
    by_channel: dict[str, list] = defaultdict(list)
    for e in entries:
        ch = get_channel(e.get("source", "unknown"))
        by_channel[ch].append(e)

    sessions = []
    for channel, channel_entries in by_channel.items():
        channel_entries.sort(key=lambda e: e.get("timestamp", ""))
        current = [channel_entries[0]]
        for entry in channel_entries[1:]:
            prev_ts = datetime.fromisoformat(current[-1]["timestamp"])
            curr_ts = datetime.fromisoformat(entry["timestamp"])
            if (curr_ts - prev_ts).total_seconds() >= GAP_THRESHOLD_SECS:
                sessions.append((channel, current))
                current = [entry]
            else:
                current.append(entry)
        sessions.append((channel, current))

    sessions.sort(key=lambda s: s[1][0].get("timestamp", ""))
    return sessions


def summarize_session(entries: list) -> dict:
    lines = []
    for e in entries:
        ts = datetime.fromisoformat(e["timestamp"]).strftime("%H:%M")
        src = e.get("source", "")
        user = e.get("user", "")[:300]
        response = e.get("claude", "")[:300]
        lines.append(f"[{ts}] ({src})\nUser: {user}\nResponse: {response}")

    exchanges = "\n\n".join(lines)
    prompt = (
        "Summarize these conversation exchanges in 1-2 sentences.\n"
        "Then list keywords — but ONLY things not already mentioned in your summary: "
        "e.g. specific file names, function names, error codes, package names, CLI flags, "
        "identifiers. If the summary already covers the key concepts, leave keywords empty. "
        "Do not pad with words that are already in the summary text.\n"
        'Return ONLY valid JSON with no extra text: {"summary": "...", "keywords": ["...", ...]}\n\n'
        f"Exchanges:\n{exchanges}"
    )

    try:
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION")}
        env["CLAUDE_SOURCE"] = "system"
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        text = (result.stdout or "").strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception:
        pass

    return {"summary": "Summary unavailable.", "keywords": []}


def main():
    now = datetime.now()
    start = now - timedelta(hours=LOOKBACK_HOURS)

    entries = load_history_range(start, now)
    entries = [e for e in entries if not is_meta_entry(e)]

    if not entries:
        return

    sessions = group_into_sessions(entries)
    existing = load_summaries_by_start()

    for channel, session in sessions:
        session_start = session[0]["timestamp"]
        session_end = session[-1]["timestamp"]

        existing_summary = existing.get(session_start)

        # Skip if nothing new since last summary AND summary is valid
        if (existing_summary
                and existing_summary.get("summary", "") not in ("", "Summary unavailable.")
                and existing_summary.get("end", "") >= session_end):
            continue

        result = summarize_session(session)
        sources = list(dict.fromkeys(e.get("source", "") for e in session))

        # Use Claude Code session ID if available, otherwise preserve existing or generate new
        session_ids = [e["session_id"] for e in session if e.get("session_id")]
        if session_ids:
            from collections import Counter
            sid = Counter(session_ids).most_common(1)[0][0]
        elif existing_summary:
            sid = existing_summary["uuid"]
        else:
            sid = str(uuid.uuid4())

        summary = {
            "uuid": sid,
            "date": datetime.fromisoformat(session_start).strftime("%Y-%m-%d"),
            "channel": channel,
            "start": session_start,
            "end": session_end,
            "sources": sources,
            "entry_count": len(session),
            "summary": result.get("summary", ""),
            "keywords": result.get("keywords", []),
        }

        save_or_update_summary(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
