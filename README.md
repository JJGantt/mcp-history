# mcp-history

MCP server for conversation history. Provides semantic search, session summarization, and unified access to exchanges across all channels (Telegram, HTTP, voice, Codex).

## Overview

**mcp-history** is a unified history system that:

- Logs every user↔Claude exchange to per-day JSON files
- Groups exchanges into sessions and generates Haiku summaries
- Indexes history via ChromaDB for semantic search
- Exposes 6 MCP tools for querying history with different granularities

Perfect for retrieving context, understanding what you've been working on, or finding past solutions to similar problems.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `get_summaries(date, end_date?)` | **Start here** — Compact session summaries with timestamps and keywords |
| `get_session(uuid)` | Full raw entries for a summary session by UUID |
| `get_history(date, end_date?, limit?)` | List entries with previews for a date range (default limit 20) |
| `get_response(date, entry_id)` | Full response text for a specific entry |
| `get_trace(date, entry_id)` | Full tool call, reasoning, and usage trace for a specific entry |
| `search_history(query, start, end, recency_weight?)` | Semantic search via ChromaDB (0.0=pure similarity, 1.0=pure recency) |

## Architecture

- `history_io.py` — Load/merge history files, find summaries by UUID
- `summarize.py` — Haiku-based session summarization (groups by 30-min gaps)
- `index_history.py` — ChromaDB indexing (reads raw entries, stores embeddings)
- `mcp_server.py` — MCP server entry point
- `config.py` — Path configuration (reads from config.json)

## Data Format

Per-day history files: `/home/jaredgantt/data/history/YYYY-MM-DD.json`

```json
[
  {
    "id": 1,
    "timestamp": "2026-03-01T10:30:45",
    "source": "claude-mac",
    "user_prompt": "...",
    "assistant_response": "..."
  },
  { "id": 2, ... }
]
```

Session summaries: `/home/jaredgantt/data/summaries/YYYY-MM-DD.json`

```json
{
  "sessions": [
    {
      "uuid": "abc-def-123",
      "start_timestamp": "2026-03-01T10:00:00",
      "end_timestamp": "2026-03-01T10:45:00",
      "sources": ["claude-mac", "claude-pi"],
      "entry_count": 12,
      "summary": "Built and tested the home automation API...",
      "keywords": ["api", "home-control", "testing"]
    }
  ]
}
```

## Config

`config.json` specifies paths (no hardcoded machine-specific values):

```json
{
  "history_dir": "/home/jaredgantt/data/history",
  "summaries_dir": "/home/jaredgantt/data/summaries",
  "chromadb_dir": "/home/jaredgantt/data/chromadb"
}
```

## Usage Tips

- **For "what was I just talking about?"** → Use `get_summaries(today)` first. It's fast and grouped by session.
- **For topic searches** → Use `search_history()` with `recency_weight: 0.0` for pure semantic similarity.
- **For recent activity** → Use `search_history()` with `recency_weight: 0.8` to bias toward recent.

## Related

- **Data:** [mcp-data](https://github.com/JJGantt/mcp-data) — Lists, reminders, notes
- **Logging:** Entries logged in real-time by Stop hooks in Claude Code sessions
