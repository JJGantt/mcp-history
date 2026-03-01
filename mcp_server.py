#!/usr/bin/env python3
"""
Unified MCP server for conversation history.

Exposes 6 tools for reading history data:
  - get_summaries: Compact session summaries (start here for recent activity)
  - get_session: Full raw entries for a summary session by UUID
  - get_history: List entries with previews for a date range
  - get_response: Full response text for a specific entry
  - get_trace: Full tool call / reasoning trace for a specific entry
  - search_history: Semantic search via ChromaDB

All paths are read from config.json — no hardcoded machine-specific values.
"""

import json
import logging
from datetime import datetime, timedelta

from mcp.server import Server
import mcp.server.stdio
import mcp.types as types

from config import HISTORY_DIR, CHROMADB_DIR
from history_io import (
    load_history_range,
    load_summaries_range,
    find_summary_by_uuid,
)

log = logging.getLogger(__name__)

server = Server("history")

DEFAULT_LIMIT = 20
_collection = None


# ---------------------------------------------------------------------------
# ChromaDB index — lazy loaded, read-only
# ---------------------------------------------------------------------------

def _get_collection():
    """Open the existing ChromaDB index. Rebuilt separately by index_history.py."""
    global _collection
    if _collection is None:
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
        _collection = client.get_or_create_collection("history")
    return _collection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_entries_lightweight(entries: list, offset: int = 0) -> str:
    if not entries:
        return "No history found for that period."
    lines = []
    for i, e in enumerate(entries):
        entry_id = offset + i
        ts = datetime.fromisoformat(e["timestamp"]).strftime("%Y-%m-%d %H:%M")
        src = e.get("source", "unknown")
        tool_flag = " [has tools]" if e.get("has_tool_use") else ""
        user_preview = e["user"][:200] + ("..." if len(e["user"]) > 200 else "")
        lines.append(f"[{entry_id}] [{ts}] ({src}){tool_flag} {user_preview}")
    return "\n".join(lines)


def parse_date(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {s!r}. Use YYYY-MM-DD.")


def get_limit(arguments: dict, default: int = DEFAULT_LIMIT) -> int:
    try:
        val = int(arguments.get("limit", default))
        return max(1, val)
    except (TypeError, ValueError):
        return default


def _resolve_entries(arguments: dict) -> tuple[list, str | None]:
    try:
        start = parse_date(arguments["date"])
    except ValueError as e:
        return [], str(e)
    end_str = arguments.get("end_date")
    end = parse_date(end_str) if end_str else start
    end = end.replace(hour=23, minute=59, second=59)
    return load_history_range(start, end), None


def _get_entry(arguments: dict) -> tuple[dict | None, int, str | None]:
    entries, err = _resolve_entries(arguments)
    if err:
        return None, -1, err
    entry_id = arguments.get("entry_id", -1)
    if not isinstance(entry_id, int) or entry_id < 0 or entry_id >= len(entries):
        return None, entry_id, f"Invalid entry_id {entry_id}. Valid range: 0-{len(entries)-1}."
    return entries[entry_id], entry_id, None


def _format_trace(trace: list) -> str:
    lines = []
    for item in trace:
        if isinstance(item, dict):
            typ = item.get("type", "")
            if typ == "assistant":
                content = item.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        bt = block.get("type", "")
                        if bt == "tool_use":
                            name = block.get("name", "?")
                            inp = json.dumps(block.get("input", {}), indent=2)
                            lines.append(f"[tool_call] {name}\n{inp}")
                        elif bt == "text":
                            lines.append(f"[assistant] {block.get('text', '')}")
                        elif bt == "thinking":
                            lines.append(f"[thinking] {block.get('thinking', '')}")
            elif typ == "user":
                content = item.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        bt = block.get("type", "")
                        if bt == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_content = "\n".join(
                                    b.get("text", "") for b in result_content
                                    if b.get("type") == "text"
                                )
                            lines.append(f"[tool_result] {str(result_content)[:2000]}")
            elif typ == "mcp_tool_call":
                tool = item.get("tool", "?")
                args = json.dumps(item.get("arguments", {}), ensure_ascii=False)
                status = item.get("status", "unknown")
                result = str(item.get("result", ""))[:800]
                lines.append(f"[mcp_tool_call] {tool} status={status}\nargs={args}\nresult={result}")
            elif typ == "command_execution":
                cmd = item.get("command", "")
                rc = item.get("exit_code")
                out = str(item.get("aggregated_output", ""))[:800]
                lines.append(f"[command_execution] rc={rc} cmd={cmd}\n{out}")
            elif typ in ("reasoning", "agent_message", "function_call", "tool_call"):
                text = item.get("text", "")
                lines.append(f"[{typ}] {text[:2000]}")
            elif typ == "turn.completed":
                usage = item.get("usage", {})
                if usage:
                    lines.append(f"[usage] input={usage.get('input_tokens',0)} output={usage.get('output_tokens',0)}")
    return "\n\n".join(lines) if lines else "No trace data available."


def _format_summaries(summaries: list) -> str:
    if not summaries:
        return "No summaries found for that period."
    lines = []
    for s in summaries:
        try:
            start = datetime.fromisoformat(s["start"]).strftime("%Y-%m-%d %H:%M")
            end = datetime.fromisoformat(s["end"]).strftime("%H:%M")
        except Exception:
            start = s.get("start", "?")
            end = "?"
        uid = s.get("uuid", "?")[:8]
        sources = ", ".join(s.get("sources", []))
        count = s.get("entry_count", "?")
        summary = s.get("summary", "")
        keywords = ", ".join(s.get("keywords", []))
        lines.append(
            f"[{uid}] {start}\u2192{end} ({sources}, {count} exchanges)\n"
            f"  {summary}\n"
            f"  Keywords: {keywords}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    date_props = {
        "date": {
            "type": "string",
            "description": "Date in YYYY-MM-DD format.",
        },
        "end_date": {
            "type": "string",
            "description": "Optional end date for a range.",
        },
        "entry_id": {
            "type": "integer",
            "description": "Entry ID from get_history or search_history.",
        },
    }
    return [
        types.Tool(
            name="get_summaries",
            description=(
                "START HERE — always call this first when you need context about past "
                "conversations. Returns Haiku-generated session summaries grouped by source "
                "channel. A full week fits in ~200 lines. Each session has a UUID — use "
                "get_session(uuid) to see the entries, then get_response/get_trace for details. "
                "Date defaults to last 7 days if not specified. "
                "IMPORTANT: When the user asks about recent or today's conversations (e.g. "
                "'what was I talking about with X', 'what did we discuss about Y'), always "
                "pass TODAY's date explicitly — do not rely on the 7-day default. Recency-"
                "implied questions always mean today first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format. Defaults to 7 days ago if omitted.",
                    },
                    "end_date": date_props["end_date"],
                    "limit": {
                        "type": "integer",
                        "description": "Max summaries to return (default 50).",
                    },
                },
            },
        ),
        types.Tool(
            name="get_session",
            description=(
                "Get all raw history entries for a specific session by UUID. "
                "Use after get_summaries to drill into a session of interest. "
                "Returns entries in the same format as get_history. "
                "Use get_response or get_trace for individual entries within the session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "description": "Full or partial UUID from get_summaries output.",
                    },
                },
                "required": ["uuid"],
            },
        ),
        types.Tool(
            name="get_history",
            description=(
                "Raw entry listing for a date range. Prefer get_summaries for overview. "
                "Use this only for entries not yet summarized or when you need the raw "
                "chronological view with entry IDs. Returns user messages with entry IDs. "
                "Entries marked [has tools] have trace data via get_trace. "
                "Use get_response for the full text reply."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": date_props["date"],
                    "end_date": date_props["end_date"],
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (default 20). Returns the most recent N.",
                    },
                },
                "required": ["date"],
            },
        ),
        types.Tool(
            name="get_response",
            description=(
                "Get Claude's full text response for a specific history entry. "
                "Use the entry ID from get_history or search_history. "
                "Date params must match the original query."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": date_props["date"],
                    "end_date": date_props["end_date"],
                    "entry_id": date_props["entry_id"],
                },
                "required": ["date", "entry_id"],
            },
        ),
        types.Tool(
            name="get_trace",
            description=(
                "Get the full conversation trace for a specific history entry — "
                "includes tool calls, tool results, reasoning/thinking, and usage stats. "
                "Only available for entries marked [has tools] in get_history output. "
                "Use when you need to see what tools were called, what they returned, "
                "or the reasoning process behind a response."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": date_props["date"],
                    "end_date": date_props["end_date"],
                    "entry_id": date_props["entry_id"],
                },
                "required": ["date", "entry_id"],
            },
        ),
        types.Tool(
            name="search_history",
            description=(
                "Semantic search via vector embeddings — use when get_summaries doesn't "
                "surface what you need, or when searching a very wide time range by meaning. "
                "WARNING: ranks by semantic similarity NOT recency — will surface old matching "
                "content over recent content. Always try get_summaries(today) first for "
                "recent questions before falling back to this tool. "
                "Finds results by semantic similarity, not just keywords. "
                "Returns matching entries with entry IDs. Entries marked [has tools] have "
                "trace data via get_trace. Use get_response for the full reply. "
                "The 'source' filter is ONLY for when the user explicitly mentions a channel "
                "or specific bot (e.g. 'Opus bot', 'from Telegram', 'on my Mac', 'from voice')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic or phrase to search for semantically.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start of search range in YYYY-MM-DD format.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End of search range in YYYY-MM-DD format.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 20).",
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Only use when user explicitly specifies a channel or bot. "
                            "Exact bot sources: opus-telegram, sonnet-telegram, haiku-telegram, "
                            "pi-telegram, claude-mac, codex-mac, claude-voice, claude-pi. "
                            "Group aliases: mac (claude-mac + codex-mac), telegram (all *-telegram), "
                            "pi (claude-pi + codex-pi), voice (claude-voice). "
                            "When user says 'Opus bot' or 'talking to Opus', use 'opus-telegram'."
                        ),
                    },
                },
                "required": ["query", "start_date", "end_date"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# MCP tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "get_summaries":
        try:
            date_str = arguments.get("date")
            if date_str:
                start = parse_date(date_str)
            else:
                start = datetime.now() - timedelta(days=7)
            end_str = arguments.get("end_date")
            end = parse_date(end_str) if end_str else datetime.now()
            end = end.replace(hour=23, minute=59, second=59)
        except ValueError as e:
            return [types.TextContent(type="text", text=str(e))]
        limit = get_limit(arguments, default=50)
        summaries = load_summaries_range(start, end)
        if len(summaries) > limit:
            summaries = summaries[-limit:]
        return [types.TextContent(type="text", text=_format_summaries(summaries))]

    if name == "get_session":
        uid = arguments.get("uuid", "").strip()
        if not uid:
            return [types.TextContent(type="text", text="uuid is required.")]
        summary = find_summary_by_uuid(uid)
        if not summary:
            return [types.TextContent(type="text", text=f"No session found with UUID '{uid}'.")]
        try:
            start = datetime.fromisoformat(summary["start"])
            end = datetime.fromisoformat(summary["end"])
        except Exception as e:
            return [types.TextContent(type="text", text=f"Invalid session timestamps: {e}")]
        entries = load_history_range(start, end)
        sources = set(summary.get("sources", []))
        if sources:
            entries = [e for e in entries if e.get("source") in sources]
        return [types.TextContent(type="text", text=format_entries_lightweight(entries))]

    if name == "get_history":
        entries, err = _resolve_entries(arguments)
        if err:
            return [types.TextContent(type="text", text=err)]
        limit = get_limit(arguments)
        if len(entries) > limit:
            offset = len(entries) - limit
            entries = entries[-limit:]
        else:
            offset = 0
        return [types.TextContent(type="text", text=format_entries_lightweight(entries, offset=offset))]

    if name == "get_response":
        entry, _, err = _get_entry(arguments)
        if err:
            return [types.TextContent(type="text", text=err)]
        response = entry.get("claude", "")
        MAX_RESPONSE = 10000
        if len(response) > MAX_RESPONSE:
            response = response[:MAX_RESPONSE] + f"\n\n[TRUNCATED — {len(response):,} total chars, showing first {MAX_RESPONSE:,}]"
        return [types.TextContent(type="text", text=response)]

    if name == "get_trace":
        entry, _, err = _get_entry(arguments)
        if err:
            return [types.TextContent(type="text", text=err)]
        trace = entry.get("trace", [])
        trace_text = _format_trace(trace)
        MAX_TRACE = 20000
        if len(trace_text) > MAX_TRACE:
            trace_text = trace_text[:MAX_TRACE] + f"\n\n[TRUNCATED — {len(trace_text):,} total chars, showing first {MAX_TRACE:,}]"
        return [types.TextContent(type="text", text=trace_text)]

    if name == "search_history":
        query = arguments.get("query", "").strip()
        if not query:
            return [types.TextContent(type="text", text="Query cannot be empty.")]
        try:
            start = parse_date(arguments["start_date"])
            end = parse_date(arguments["end_date"]).replace(hour=23, minute=59, second=59)
        except (ValueError, KeyError) as e:
            return [types.TextContent(type="text", text=str(e))]

        limit = get_limit(arguments)
        source = arguments.get("source", "").strip().lower()

        source_where = None
        if source:
            source_map = {
                "mac": ["claude-mac", "codex-mac"],
                "telegram": ["claude-telegram", "codex-telegram", "opus-telegram", "sonnet-telegram", "haiku-telegram", "pi-telegram"],
                "pi": ["claude-pi", "codex-pi"],
                "voice": ["claude-voice"],
            }
            sources = source_map.get(source, [source])
            source_where = {"source": {"$eq": sources[0]}} if len(sources) == 1 else {"source": {"$in": sources}}

        start_unix = int(start.timestamp())
        end_unix = int(end.timestamp())
        date_where = {"$and": [
            {"timestamp_unix": {"$gte": start_unix}},
            {"timestamp_unix": {"$lte": end_unix}},
        ]}

        where = {"$and": [date_where, source_where]} if source_where else date_where

        collection = _get_collection()
        total = collection.count()
        if total == 0:
            return [types.TextContent(type="text", text="No history indexed yet.")]

        n_results = min(limit, total)
        try:
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
        except Exception:
            try:
                results = collection.query(query_texts=[query], n_results=min(limit * 3, total))
                filtered_ids, filtered_meta, filtered_dist = [], [], []
                for doc_id, meta, dist in zip(results["ids"][0], results["metadatas"][0], results["distances"][0]):
                    if start_unix <= meta.get("timestamp_unix", 0) <= end_unix:
                        filtered_ids.append(doc_id)
                        filtered_meta.append(meta)
                        filtered_dist.append(dist)
                results = {"ids": [filtered_ids[:limit]], "metadatas": [filtered_meta[:limit]], "distances": [filtered_dist[:limit]]}
            except Exception as e2:
                return [types.TextContent(type="text", text=f"Search error: {e2}")]

        ids = results["ids"][0]
        metas = results["metadatas"][0]
        if not ids:
            return [types.TextContent(type="text", text=f"No matches for '{query}'.")]

        lines = []
        for meta in metas:
            ts_str = meta.get("timestamp", "")
            try:
                ts_fmt = datetime.fromisoformat(ts_str).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_fmt = ts_str
            day_index = meta["day_index"]
            src = meta.get("source", "unknown")
            tool_flag = " [has tools]" if meta.get("has_tool_use") == "true" else ""
            preview = meta.get("user_preview", "")
            if len(preview) == 300:
                preview += "..."
            lines.append(f"[{day_index}] [{ts_fmt}] ({src}){tool_flag} {preview}")

        return [types.TextContent(type="text", text="\n".join(lines))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
