"""
History file I/O — read, write, and append to per-day JSON history files.

All modules use this for history access instead of direct file manipulation.
Uses fcntl file locking for safe concurrent writes (needed when multiple
processes write to the same day file, e.g. bot + receiver on Pi).
"""

import fcntl
import json
from datetime import datetime, timedelta
from pathlib import Path

from config import HISTORY_DIR, SUMMARIES_DIR


def day_file(date: datetime) -> Path:
    return HISTORY_DIR / f"{date.strftime('%Y-%m-%d')}.json"


def load_day(date: datetime) -> list:
    f = day_file(date)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


def load_history_range(start: datetime, end: datetime) -> list:
    results = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_day:
        for entry in load_day(day):
            try:
                ts = datetime.fromisoformat(entry["timestamp"]).replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            if start <= ts <= end:
                results.append(entry)
        day += timedelta(days=1)
    results.sort(key=lambda e: e.get("timestamp", ""))
    return results


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def append_entry(source: str, user_msg: str, assistant_msg: str,
                 timestamp: datetime | None = None, **extra) -> None:
    """Append a single exchange to today's history file with file locking."""
    now = timestamp or datetime.now()
    target = day_file(now)

    entry = {
        "timestamp": now.isoformat(timespec="seconds"),
        "source": source,
        "user": user_msg,
        "claude": assistant_msg,
    }
    entry.update(extra)

    with open(target, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read().strip()
            entries = json.loads(content) if content else []
        except (json.JSONDecodeError, ValueError):
            entries = []
        entries.append(entry)
        # Rewrite atomically while holding lock
        write_json_atomic(target, entries)
        fcntl.flock(f, fcntl.LOCK_UN)


def load_summaries_range(start: datetime, end: datetime) -> list:
    results = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_day:
        f = SUMMARIES_DIR / f"{day.strftime('%Y-%m-%d')}.json"
        if f.exists():
            try:
                for s in json.loads(f.read_text()):
                    try:
                        ts = datetime.fromisoformat(s.get("start", "1970-01-01"))
                        ts = ts.replace(tzinfo=None)
                    except (ValueError, TypeError):
                        continue
                    if start <= ts <= end:
                        results.append(s)
            except Exception:
                pass
        day += timedelta(days=1)
    results.sort(key=lambda s: s.get("start", ""))
    return results


def find_summary_by_uuid(uid: str) -> dict | None:
    for f in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            for s in json.loads(f.read_text()):
                if s.get("uuid") == uid:
                    return s
        except Exception:
            pass
    # Try partial match
    all_summaries = []
    for f in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            all_summaries.extend(json.loads(f.read_text()))
        except Exception:
            pass
    matches = [s for s in all_summaries if s.get("uuid", "").startswith(uid)]
    if len(matches) == 1:
        return matches[0]
    return None
