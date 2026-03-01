"""
Configuration loader for mcp-history.

Loads config.json from the repo directory, expands ~ in path fields,
and provides a singleton CONFIG dict used by all other modules.
"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _expand_paths(cfg: dict) -> dict:
    """Expand ~ in known path fields."""
    for key in ("data_dir",):
        if key in cfg:
            cfg[key] = str(Path(cfg[key]).expanduser())
    peer = cfg.get("peer", {})
    if "data_dir" in peer:
        peer["data_dir"] = str(Path(peer["data_dir"]).expanduser())
    if "ssh_key" in peer:
        peer["ssh_key"] = str(Path(peer["ssh_key"]).expanduser())
    return cfg


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No config.json found at {_CONFIG_PATH}. "
            f"Copy config.example.json to config.json and fill in your values."
        )
    with open(_CONFIG_PATH) as f:
        cfg = json.load(f)
    return _expand_paths(cfg)


CONFIG = load_config()

# Derived paths
HISTORY_DIR = Path(CONFIG["data_dir"]) / "history"
SUMMARIES_DIR = Path(CONFIG["data_dir"]) / "summaries"
CHROMADB_DIR = Path(CONFIG["data_dir"]) / "history-chromadb"

# Ensure directories exist
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
CHROMADB_DIR.mkdir(parents=True, exist_ok=True)

# Source → channel mapping (shared by summarizer, context builder, etc.)
CHANNEL_MAP = {
    "claude-mac": "mac", "codex-mac": "mac",
    "claude-telegram": "telegram", "codex-telegram": "telegram",
    "opus-telegram": "telegram", "sonnet-telegram": "telegram",
    "haiku-telegram": "telegram", "pi-telegram": "telegram",
    "claude-http": "http", "codex-http": "http",
    "claude-voice": "voice",
    "claude-pi": "pi", "codex-pi": "pi",
}


def get_channel(source: str) -> str:
    return CHANNEL_MAP.get(source, source)
