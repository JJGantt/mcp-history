#!/bin/bash
# inotifywait watcher for Linux/Pi — triggers sync.py push on data changes.
# Not used on macOS (launchd WatchPaths handles this instead).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCH_DIR="$(python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); from config import CONFIG; print(CONFIG['data_dir'])")"
PYTHON="${PYTHON:-python3}"

if ! command -v inotifywait &>/dev/null; then
    echo "inotifywait not found. Install inotify-tools." >&2
    exit 1
fi

inotifywait -m -r -e close_write,create,move,delete "$WATCH_DIR" | while read -r _; do
    "$PYTHON" "$SCRIPT_DIR/sync.py" push
    sleep 2
done
