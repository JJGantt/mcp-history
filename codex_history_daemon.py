#!/usr/bin/env python3
"""
Codex history logger for macOS.

Watches ~/.codex/sessions JSONL files and appends user+assistant exchanges
into ~/pi-data/history/YYYY-MM-DD.json so the local MCP can read them.
"""

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
STATE_PATH = Path.home() / ".codex" / "codex_history_state.json"
HISTORY_DIR = Path.home() / "pi-data" / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

SLEEP_SECS = 2

PI_HOSTS = ["raspberrypi.local", "100.104.197.58"]
PI_PORT = 8765


def _post_to_pi(user_msg: str, assistant_msg: str, source: str, timestamp: str) -> None:
    """POST exchange to Pi /log endpoint for real-time sync."""
    body = json.dumps({
        "user": user_msg,
        "claude": assistant_msg,
        "source": source,
        "timestamp": timestamp,
    }).encode()
    for host in PI_HOSTS:
        try:
            req = urllib.request.Request(
                f"http://{host}:{PI_PORT}/log",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            return
        except Exception:
            continue


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"files": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"files": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _parse_event_time(raw) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _append_history(
    user_msg: str,
    assistant_msg: str,
    source: str = "codex-mac",
    event_time: Optional[datetime] = None,
    turn_id: str = "",
    turn_message_index: Optional[int] = None,
    session_path: str = "",
) -> None:
    now = event_time or datetime.now()
    day_file = HISTORY_DIR / f"{now.strftime('%Y-%m-%d')}.json"
    entries = []
    if day_file.exists():
        try:
            entries = json.loads(day_file.read_text())
        except Exception:
            entries = []

    # Dedupe replayed turns safely.
    if turn_id:
        for e in entries:
            if e.get("source") != source or e.get("turn_id") != turn_id:
                continue
            if turn_message_index is not None:
                if e.get("turn_message_index") == turn_message_index:
                    return
            elif e.get("user") == user_msg and e.get("claude") == assistant_msg:
                return

    ts = now.isoformat(timespec="seconds")
    record = {
        "timestamp": ts,
        "source": source,
        "user": user_msg,
        "claude": assistant_msg,
        "turn_id": turn_id,
        "session_path": session_path,
    }
    if turn_message_index is not None:
        record["turn_message_index"] = turn_message_index
    entries.append(record)
    _write_json_atomic(day_file, entries)

    # Also POST to Pi for real-time availability
    try:
        _post_to_pi(user_msg, assistant_msg, source, ts)
    except Exception:
        pass


def _extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("input_text", "output_text"):
                    parts.append(block.get("text", ""))
                elif block.get("type") == "text":
                    parts.append(block.get("text", ""))
        return " ".join(p for p in parts if p).strip()
    return ""


def _iter_session_files() -> list[Path]:
    if not SESSIONS_DIR.exists():
        return []
    return sorted(SESSIONS_DIR.rglob("*.jsonl"))


def _queue_user_message(file_state: dict, message: str, raw_timestamp: Optional[str]) -> None:
    """Queue a user message for pairing on task_complete."""
    msg = (message or "").strip()
    if not msg:
        return
    pending = file_state.setdefault("pending_users", [])
    raw_ts = raw_timestamp or ""
    if pending:
        last = pending[-1]
        if last.get("message") == msg:
            last_ts = _parse_event_time(last.get("raw_ts", ""))
            this_ts = _parse_event_time(raw_ts)
            # Same message from multiple event streams (response_item + event_msg)
            # typically lands within milliseconds; keep only one.
            if last_ts is None or this_ts is None:
                return
            if abs((this_ts - last_ts).total_seconds()) <= 2:
                return
    pending.append({"message": msg, "raw_ts": raw_ts})


def _process_file(path: Path, state: dict) -> None:
    files_state = state.setdefault("files", {})
    key = str(path)
    file_state = files_state.get(key)

    # On first discovery, start tailing from EOF so we don't backfill old sessions.
    if file_state is None:
        try:
            size = path.stat().st_size
        except Exception:
            return
        files_state[key] = {"offset": size, "pending_users": [], "current_turn_id": ""}
        return

    try:
        size = path.stat().st_size
    except Exception:
        return

    offset = file_state.get("offset", 0)
    if offset > size:
        offset = 0

    if size == offset:
        return

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                entry_time = _parse_event_time(entry.get("timestamp"))
                typ = entry.get("type")
                payload = entry.get("payload", {})

                if typ == "event_msg":
                    ptype = payload.get("type")

                    if ptype == "task_started":
                        file_state["pending_users"] = []
                        file_state["current_turn_id"] = payload.get("turn_id", "")
                        continue

                    if ptype == "user_message":
                        _queue_user_message(file_state, payload.get("message", ""), entry.get("timestamp"))
                        continue

                    if ptype == "task_complete":
                        assistant = payload.get("last_agent_message", "")
                        pending = file_state.get("pending_users", [])
                        turn_id = payload.get("turn_id", "") or file_state.get("current_turn_id", "")
                        if assistant and pending:
                            for idx, item in enumerate(pending):
                                msg = item.get("message", "")
                                user_time = _parse_event_time(item.get("raw_ts", "")) or entry_time
                                _append_history(
                                    msg,
                                    assistant,
                                    event_time=user_time,
                                    turn_id=turn_id,
                                    turn_message_index=idx,
                                    session_path=str(path),
                                )
                        file_state["pending_users"] = []
                        file_state["current_turn_id"] = ""
                        continue

                if typ == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    content = payload.get("content", [])
                    msg = _extract_text_from_content(content)
                    _queue_user_message(file_state, msg, entry.get("timestamp"))
                    continue

            file_state["offset"] = f.tell()
    except Exception:
        return


def main() -> None:
    while True:
        state = _load_state()
        for path in _iter_session_files():
            _process_file(path, state)
        _save_state(state)
        time.sleep(SLEEP_SECS)


if __name__ == "__main__":
    main()
