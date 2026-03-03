#!/usr/bin/env python3
"""
Bidirectional sync between local and peer data directories.

Usage:
  sync.py push    # Push local data to peer
  sync.py pull    # Pull peer data to local
  sync.py both    # Pull then push (full bidirectional sync)

Reads peer SSH config and sync settings from config.json.
Includes debounce and failure counting with optional Telegram notifications.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG

_SYNC = CONFIG.get("sync", {})
_PEER = CONFIG.get("peer", {})
_DEBOUNCE = _SYNC.get("debounce_seconds", 20)
_FAIL_THRESHOLD = 3

# Platform-specific cache dir
if sys.platform == "darwin":
    _CACHE_DIR = Path.home() / "Library" / "Caches" / "mcp-history"
else:
    _CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mcp-history"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_LOCK_FILE = _CACHE_DIR / "sync.lock"
_FAIL_FILE = _CACHE_DIR / "sync_failures"

_RSYNC_EXCLUDES = [
    "history/", "lists/", "notes/", "reminders/", "health/",
    ".*.tmp", "*.tmp",
    "*.sqlite3-wal", "*.sqlite3-shm",
    "sessions.json",  # Pi-specific bot session state — must not be overwritten by sync
]


def _find_host():
    """Find reachable peer host."""
    hosts = [h for h in [_PEER.get("local_ip"), _PEER.get("tailscale_ip")] if h]
    user = _PEER.get("ssh_user", "")
    key = _PEER.get("ssh_key", "")
    for host in hosts:
        try:
            result = subprocess.run(
                ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                 "-o", "StrictHostKeyChecking=no", f"{user}@{host}", "true"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return host
        except subprocess.TimeoutExpired:
            continue
    return None


def _debounce_check() -> bool:
    """Return True if we should skip (too recent)."""
    if _LOCK_FILE.exists():
        try:
            last = _LOCK_FILE.stat().st_mtime
            if time.time() - last < _DEBOUNCE:
                return True
        except OSError:
            pass
    return False


def _debounce_update():
    _LOCK_FILE.touch()


def _count_failure():
    count = 0
    if _FAIL_FILE.exists():
        try:
            count = int(_FAIL_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    count += 1
    _FAIL_FILE.write_text(str(count))
    if count == _FAIL_THRESHOLD:
        try:
            from notify import notify
            notify(f"mcp-history sync: {count} consecutive failures reaching peer.")
        except Exception:
            pass


def _reset_failures():
    if _FAIL_FILE.exists():
        _FAIL_FILE.unlink(missing_ok=True)


def _rsync(src: str, dest: str, host: str):
    """Run rsync between local and remote."""
    user = _PEER.get("ssh_user", "")
    key = _PEER.get("ssh_key", "")
    exclude_args = []
    for exc in _RSYNC_EXCLUDES:
        exclude_args.extend(["--exclude", exc])

    cmd = [
        "rsync", "-az", "--delete",
        *exclude_args,
        "-e", f"ssh -i {key} -o BatchMode=yes -o StrictHostKeyChecking=no",
        src, dest,
    ]
    subprocess.run(cmd, capture_output=True, timeout=120)


def _run_merge(host: str):
    """Run bidirectional history merge."""
    try:
        merge_script = Path(__file__).parent / "merge.py"
        python = sys.executable
        subprocess.run(
            [python, str(merge_script), host, _PEER.get("ssh_key", "")],
            capture_output=True, timeout=60,
        )
    except Exception:
        pass


def _run_data_merge():
    """Run mcp-data merge (lists, notes, reminders, health)."""
    try:
        merge_script = Path.home() / "mcp-data" / "merge.py"
        if merge_script.exists():
            subprocess.run(
                [sys.executable, str(merge_script)],
                capture_output=True, timeout=60,
            )
    except Exception:
        pass


def push(host: str):
    """Push local data to peer."""
    local_dir = CONFIG["data_dir"].rstrip("/") + "/"
    remote_dir = _PEER["data_dir"].rstrip("/") + "/"
    user = _PEER.get("ssh_user", "")
    _rsync(local_dir, f"{user}@{host}:{remote_dir}", host)
    _run_merge(host)
    _run_data_merge()


def pull(host: str):
    """Pull peer data to local."""
    local_dir = CONFIG["data_dir"].rstrip("/") + "/"
    remote_dir = _PEER["data_dir"].rstrip("/") + "/"
    user = _PEER.get("ssh_user", "")
    _rsync(f"{user}@{host}:{remote_dir}", local_dir, host)
    _run_merge(host)
    _run_data_merge()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("push", "pull", "both"):
        print(f"Usage: {sys.argv[0]} push|pull|both", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    if _debounce_check():
        return

    host = _find_host()
    if not host:
        _count_failure()
        return

    _reset_failures()

    if action == "pull":
        pull(host)
    elif action == "push":
        push(host)
    elif action == "both":
        pull(host)
        push(host)

    _debounce_update()


if __name__ == "__main__":
    main()
