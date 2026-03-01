#!/usr/bin/env python3
"""
Haiku-powered session summarizer for conversation history.

Groups history entries into sessions by configurable gap threshold, then calls
Haiku to generate a 1-2 sentence summary + keywords for each session.

Runs as part of the Stop hook chain after hook.py.
Only summarizes sessions that ended beyond the active buffer window.
"""

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG, SUMMARIES_DIR
from history_io import load_history_range, write_json_atomic
from meta_filter import is_meta_entry

_SUMMARIZER = CONFIG.get("summarizer", {})
GAP_THRESHOLD_SECS = _SUMMARIZER.get("gap_threshold_secs", 1800)
ACTIVE_BUFFER_SECS = _SUMMARIZER.get("active_buffer_secs", 300)
LOOKBACK_HOURS = _SUMMARIZER.get("lookback_hours", 48)


def load_all_summaries() -> list:
    summaries = []
    for f in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            summaries.extend(json.loads(f.read_text()))
        except Exception:
            pass
    return summaries


def get_latest_summarized_end(summaries: list) -> datetime | None:
    if not summaries:
        return None
    latest = max(summaries, key=lambda s: s.get("end", ""))
    try:
        return datetime.fromisoformat(latest["end"])
    except Exception:
        return None


def save_summaries(new_summaries: list) -> None:
    by_date: dict[str, list] = {}
    for s in new_summaries:
        by_date.setdefault(s["date"], []).append(s)

    for date_str, day_summaries in by_date.items():
        summary_file = SUMMARIES_DIR / f"{date_str}.json"
        existing = []
        if summary_file.exists():
            try:
                existing = json.loads(summary_file.read_text())
            except Exception:
                pass
        existing.extend(day_summaries)
        write_json_atomic(summary_file, existing)


def group_into_sessions(entries: list) -> list[list]:
    if not entries:
        return []
    sessions = []
    current = [entries[0]]
    for entry in entries[1:]:
        prev_ts = datetime.fromisoformat(current[-1]["timestamp"])
        curr_ts = datetime.fromisoformat(entry["timestamp"])
        if (curr_ts - prev_ts).total_seconds() >= GAP_THRESHOLD_SECS:
            sessions.append(current)
            current = [entry]
        else:
            current.append(entry)
    sessions.append(current)
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
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
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
    existing = load_all_summaries()
    latest_end = get_latest_summarized_end(existing)

    now = datetime.now()
    start = now - timedelta(hours=LOOKBACK_HOURS)
    if latest_end and latest_end > start:
        start = latest_end + timedelta(seconds=1)

    entries = load_history_range(start, now)
    entries = [e for e in entries if not is_meta_entry(e)]

    if not entries:
        return

    sessions = group_into_sessions(entries)
    new_summaries = []

    for session in sessions:
        session_end = datetime.fromisoformat(session[-1]["timestamp"])
        if (now - session_end).total_seconds() < ACTIVE_BUFFER_SECS:
            continue

        result = summarize_session(session)
        session_start = datetime.fromisoformat(session[0]["timestamp"])
        sources = list(dict.fromkeys(e.get("source", "") for e in session))

        new_summaries.append({
            "uuid": str(uuid.uuid4()),
            "date": session_start.strftime("%Y-%m-%d"),
            "start": session[0]["timestamp"],
            "end": session[-1]["timestamp"],
            "sources": sources,
            "entry_count": len(session),
            "summary": result.get("summary", ""),
            "keywords": result.get("keywords", []),
        })

    if new_summaries:
        save_summaries(new_summaries)


if __name__ == "__main__":
    # Fork to background immediately so the Stop hook doesn't block.
    import os
    pid = os.fork()
    if pid != 0:
        sys.exit(0)  # Parent exits instantly; child does the work.
    os.setsid()      # Detach from the controlling terminal.

    try:
        main()
    except Exception:
        pass
