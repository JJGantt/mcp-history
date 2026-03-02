#!/usr/bin/env python3
"""
Lightweight HTTP receiver for peer history sync.

Accepts POST /log from a paired node's hook and writes the entry to local
history. No Flask dependency — uses stdlib http.server.

Endpoints:
  POST /log     — receive an exchange and append to local history
  GET  /status  — health check
"""

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from history_io import append_entry
from datetime import datetime


class HistoryHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/log":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                user = body.get("user", "")
                claude = body.get("claude", "")
                source = body.get("source", "unknown")
                ts_str = body.get("timestamp")
                ts = datetime.fromisoformat(ts_str) if ts_str else None
                extra = {k: v for k, v in body.items() if k not in ("user", "claude", "source", "timestamp")}
                append_entry(source, user, claude, timestamp=ts, **extra)
                self._respond(200, {"ok": True})
            except Exception as e:
                self._respond(500, {"ok": False, "error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def do_GET(self):
        if self.path == "/status":
            self._respond(200, {
                "ok": True,
                "machine": CONFIG.get("machine_name", "unknown"),
            })
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default access logs


def main():
    receiver = CONFIG.get("receiver", {})
    host = receiver.get("host", "0.0.0.0")
    port = receiver.get("port", 8766)
    server = HTTPServer((host, port), HistoryHandler)
    print(f"History receiver listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
