"""Пути, конфиг и атомарная запись файлов."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "ai-free"

DATA_DIR = Path(os.environ.get("AIFREE_HOME", Path.home() / ".local" / "share" / APP_NAME))
CHATS_DIR = DATA_DIR / "chats"
EXPORTS_DIR = DATA_DIR / "exports"
BACKUPS_DIR = DATA_DIR / "backups"
HISTORY_FILE = DATA_DIR / "history"
MEMORY_FILE = DATA_DIR / "memory.json"
CONFIG_FILE = Path(
    os.environ.get("AIFREE_CONFIG", Path.home() / ".config" / APP_NAME / "config.json")
)

DEFAULTS = {
    "model": "auto",
    "stream": True,
    "system": "",
    "last_chat": None,
    "max_context_chars": 14000,
    "summary_after_messages": 16,
    "summary_keep_recent": 6,
    "timeout": 60,
    "agent_workspace": "",
    "agent_mode": "ask",  # ask | auto | dry-run
    "agent_chat": False,  # обычные сообщения отправлять агенту
    "smart_route": False,  # сам решать: чат или агент
    "agent_context_chars": 30000,
    "agent_max_rounds": 6,
    "agent_max_edit_rounds": 4,
    "agent_max_fix_rounds": 4,
    "agent_run_timeout": 60,
    "agent_run_output_chars": 8000,
    "agent_auto_approve": False,  # совместимость
    "agent_max_diff_lines": 120,
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, CHATS_DIR, EXPORTS_DIR, BACKUPS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    atomic_write(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
