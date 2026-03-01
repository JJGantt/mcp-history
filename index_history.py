#!/usr/bin/env python3
"""
Rebuild the ChromaDB history index.

Run on a schedule (e.g. every 5 minutes via launchd/systemd) — NOT as a hook.
Reads all history JSON files, compares entry count with the existing index,
and does a full rebuild only when the count has changed.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import HISTORY_DIR, CHROMADB_DIR
from meta_filter import is_meta_entry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def rebuild_index():
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMADB_DIR))

    all_entries = []
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
        for i, e in enumerate(filtered):
            all_entries.append((date_str, i, e))

    collection = client.get_or_create_collection("history")

    if collection.count() == len(all_entries):
        return

    log.info(f"Rebuilding history index: {len(all_entries)} entries")
    client.delete_collection("history")
    collection = client.create_collection("history")
    if all_entries:
        batch_size = 100
        for b in range(0, len(all_entries), batch_size):
            batch = all_entries[b:b + batch_size]
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
                    "timestamp": e.get("timestamp", ""),
                    "timestamp_unix": int(datetime.fromisoformat(
                        e.get("timestamp", "1970-01-01T00:00:00")
                    ).timestamp()),
                    "has_tool_use": "true" if e.get("has_tool_use") else "false",
                    "user_preview": e.get("user", "")[:300],
                } for date, i, e in batch]
            )
    log.info("Index build complete")


if __name__ == "__main__":
    try:
        rebuild_index()
    except Exception as e:
        log.error(f"Index rebuild failed: {e}")
        sys.exit(1)
