#!/usr/bin/env python3
"""
Session summarizer — runs on a schedule (every 5 minutes via launchd).

Groups entries by session_id (from Claude Code transcript filenames).
Re-summarizes if new entries have appeared since the last summary.

Run by launchd — no forking needed, no stop hook recursion possible.
"""

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG, SUMMARIES_DIR, get_channel
from history_io import load_history_range, write_json_atomic
from meta_filter import is_meta_entry

_SUMMARIZER = CONFIG.get("summarizer", {})
LOOKBACK_HOURS = _SUMMARIZER.get("lookback_hours", 48)


def load_summaries_by_uuid() -> dict:
    """Load all existing summaries keyed by UUID (session_id)."""
    summaries = {}
    for f in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            for s in json.loads(f.read_text()):
                key = s.get("uuid", "")
                if key:
                    summaries[key] = s
        except Exception:
            pass
    return summaries


def save_or_update_summary(summary: dict) -> None:
    """Write or update a single summary in its date file, matched by uuid."""
    date_str = summary["date"]
    summary_file = SUMMARIES_DIR / f"{date_str}.json"

    existing = []
    if summary_file.exists():
        try:
            existing = json.loads(summary_file.read_text())
        except Exception:
            pass

    uid = summary.get("uuid", "")
    replaced = False
    for i, s in enumerate(existing):
        if s.get("uuid") == uid:
            existing[i] = summary
            replaced = True
            break
    if not replaced:
        existing.append(summary)

    write_json_atomic(summary_file, existing)


def group_by_session_id(entries: list) -> list[tuple[str, str, list]]:
    """Group entries by session_id. Returns (channel, session_id, entries) tuples."""
    by_session: dict[str, list] = defaultdict(list)
    for e in entries:
        sid = e.get("session_id")
        if sid:
            by_session[sid].append(e)

    sessions = []
    for sid, session_entries in by_session.items():
        session_entries.sort(key=lambda e: e.get("timestamp", ""))
        sources = [e.get("source", "unknown") for e in session_entries]
        channel = get_channel(Counter(sources).most_common(1)[0][0])
        sessions.append((channel, sid, session_entries))

    sessions.sort(key=lambda s: s[2][0].get("timestamp", ""))
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

    sessions = group_by_session_id(entries)
    existing = load_summaries_by_uuid()

    for channel, sid, session in sessions:
        session_start = session[0]["timestamp"]
        session_end = session[-1]["timestamp"]

        existing_summary = existing.get(sid)

        # Skip if nothing new since last summary AND summary is valid
        if (existing_summary
                and existing_summary.get("summary", "") not in ("", "Summary unavailable.")
                and existing_summary.get("end", "") >= session_end):
            continue

        result = summarize_session(session)
        sources = list(dict.fromkeys(e.get("source", "") for e in session))

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
