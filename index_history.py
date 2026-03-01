#!/usr/bin/env python3
"""
Stop hook — rebuilds the ChromaDB history index if entry count changed.

Silently skips if the database is locked (e.g. MCP server already has it open).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from mcp_server import build_or_load_index
    build_or_load_index()
except Exception:
    sys.exit(0)
