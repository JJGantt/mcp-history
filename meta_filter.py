"""
Shared meta-entry detection.

Identifies system/context-injection entries that should be excluded from
summaries, search indexes, and context building.
"""


def is_meta_entry(entry: dict) -> bool:
    user = str(entry.get("user", "")).lstrip(" -\n")
    return (
        user.startswith("Recent conversation history")
        or user.startswith("A previous Claude subprocess")
        or user.startswith("The Codex CLI subprocess")
        or user.startswith("You are responding to")
    )
