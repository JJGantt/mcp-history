#!/usr/bin/env python3
"""
Incremental ChromaDB history indexer.

Run on a schedule (e.g. every 5 minutes via launchd/systemd) — NOT as a hook.
Only indexes new entries since last run. Tracks state via a small JSON file
to avoid re-scanning the entire collection each time.
"""

import fcntl
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import HISTORY_DIR, CHROMADB_DIR
from meta_filter import is_meta_entry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

STATE_FILE = CHROMADB_DIR / "index_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state) + "\n")


def _ensure_wal_mode():
    """Switch ChromaDB's SQLite to WAL journal mode for safe concurrent access."""
    db_path = CHROMADB_DIR / "chroma.sqlite3"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.close()
        log.info(f"SQLite journal_mode={mode}")


def update_index():
    lock_path = CHROMADB_DIR / "index.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another indexer is running, skipping")
        lock_fd.close()
        return
    try:
        _update_index_locked()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _update_index_locked():
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
    collection = client.get_or_create_collection("history")

    _ensure_wal_mode()

    # State tracks {date_str: entry_count} for files we've already indexed
    state = _load_state()

    new_entries = []
    new_state = {}
    for f in sorted(HISTORY_DIR.glob("*.json")):
        date_str = f.stem
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        try:
            raw = json.loads(f.read_text())
        except Exception:
            continue
        filtered = [e for e in raw if not is_meta_entry(e)]
        new_state[date_str] = len(filtered)

        prev_count = state.get(date_str, 0)
        if len(filtered) <= prev_count:
            continue

        # Only index entries beyond what we already have for this date
        for i, e in enumerate(filtered):
            if i < prev_count:
                continue
            new_entries.append((date_str, i, e))

    if not new_entries:
        return

    log.info(f"Indexing {len(new_entries)} new entries")
    batch_size = 100
    for b in range(0, len(new_entries), batch_size):
        batch = new_entries[b:b + batch_size]
        collection.upsert(
            ids=[f"{date}_{i}" for date, i, _ in batch],
            documents=[
                (e.get("user", "") + " " + e.get("claude", ""))[:8000]
                for _, _, e in batch
            ],
            metadatas=[{
                "date": date,
                "day_index": i,
                "source": e.get("source", "unknown"),
                "session_id": e.get("session_id", ""),
                "timestamp": e.get("timestamp", ""),
                "timestamp_unix": int(datetime.fromisoformat(
                    e.get("timestamp", "1970-01-01T00:00:00")
                ).timestamp()),
                "has_tool_use": "true" if e.get("has_tool_use") else "false",
                "user_preview": e.get("user", "")[:300],
            } for date, i, e in batch]
        )

    _save_state(new_state)
    log.info(f"Indexed {len(new_entries)} entries (total: {collection.count()})")


if __name__ == "__main__":
    try:
        update_index()
    except Exception as e:
        log.error(f"Index update failed: {e}")
        sys.exit(1)
