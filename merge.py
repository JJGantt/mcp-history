#!/usr/bin/env python3
"""
Bidirectional history merge between local and peer over SSH.

Usage:
  merge.py                    # Uses peer config from config.json
  merge.py <ssh_host> <ssh_key>  # Override host/key (legacy compat)

Merges local history dir with peer's history dir. Deduplicates entries by
(timestamp, source) and writes the merged result to both sides.
Only processes files modified in the last N days (from config).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG, HISTORY_DIR


def _peer_config():
    peer = CONFIG.get("peer", {})
    return {
        "ssh_user": peer.get("ssh_user", ""),
        "ssh_key": peer.get("ssh_key", ""),
        "data_dir": peer.get("data_dir", ""),
        "hosts": [h for h in [peer.get("local_ip"), peer.get("tailscale_ip")] if h],
    }


def _days_back():
    return CONFIG.get("sync", {}).get("merge_days_back", 7)


def ssh_cmd(host, user, key, cmd):
    result = subprocess.run(
        ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=5", f"{user}@{host}", cmd],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH failed: {cmd}\n{result.stderr.strip()}")
    return result.stdout


def list_local_files():
    if not HISTORY_DIR.is_dir():
        return set()
    return {f.name for f in HISTORY_DIR.iterdir()
            if f.suffix == ".json" and not f.name.endswith(".lock")}


def list_remote_files(host, user, key, remote_dir):
    try:
        out = ssh_cmd(host, user, key, f"ls {remote_dir}/history/*.json 2>/dev/null || true")
    except RuntimeError:
        return set()
    return {os.path.basename(line.strip()) for line in out.splitlines() if line.strip()}


def is_recent(filename, days):
    try:
        date_str = filename.replace(".json", "")
        file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        cutoff = (datetime.now() - timedelta(days=days)).date()
        return file_date >= cutoff
    except ValueError:
        return False


def read_local(filename):
    path = HISTORY_DIR / filename
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def read_remote(host, user, key, remote_dir, filename):
    try:
        out = ssh_cmd(host, user, key, f"cat {remote_dir}/history/{filename}")
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except (RuntimeError, json.JSONDecodeError):
        return []


def entry_key(entry):
    return (entry.get("timestamp", ""), entry.get("source", ""))


def merge_entries(local, remote):
    seen = {}
    for entry in local + remote:
        key = entry_key(entry)
        if key not in seen:
            seen[key] = entry
    return sorted(seen.values(), key=lambda e: e.get("timestamp", ""))


def write_local(filename, entries):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / filename
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.rename(path)


def write_remote(host, user, key, remote_dir, filename, entries):
    payload = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    remote_path = f"{remote_dir}/history"
    proc = subprocess.run(
        ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         f"{user}@{host}",
         f"cat > {remote_path}/{filename}.tmp && mv {remote_path}/{filename}.tmp {remote_path}/{filename}"],
        input=payload, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to write remote {filename}: {proc.stderr.strip()}")


def find_reachable_host(hosts, user, key):
    for host in hosts:
        try:
            ssh_cmd(host, user, key, "true")
            return host
        except (RuntimeError, subprocess.TimeoutExpired):
            continue
    return None


def main():
    if len(sys.argv) == 3:
        host, key = sys.argv[1], sys.argv[2]
        user = CONFIG.get("peer", {}).get("ssh_user", os.environ.get("USER", ""))
        remote_dir = CONFIG.get("peer", {}).get("data_dir", "")
    else:
        pcfg = _peer_config()
        user = pcfg["ssh_user"]
        key = pcfg["ssh_key"]
        remote_dir = pcfg["data_dir"]
        host = find_reachable_host(pcfg["hosts"], user, key)
        if not host:
            return

    days = _days_back()
    local_files = list_local_files()
    remote_files = list_remote_files(host, user, key, remote_dir)
    all_files = local_files | remote_files
    recent = sorted(f for f in all_files if is_recent(f, days))

    if not recent:
        return

    for filename in recent:
        local_entries = read_local(filename)
        remote_entries = read_remote(host, user, key, remote_dir, filename)

        if not local_entries and not remote_entries:
            continue

        merged = merge_entries(local_entries, remote_entries)

        if len(merged) == len(local_entries) == len(remote_entries):
            continue

        write_local(filename, merged)
        write_remote(host, user, key, remote_dir, filename, merged)


if __name__ == "__main__":
    main()
