"""Пути, конфиг и атомарная запись файлов."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "ExelCode"


def _default_home() -> Path:
    """Data directory: XDG on Unix, LocalAppData on Windows."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME.lower()


def _default_config_file() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / APP_NAME / "config.json"
    return Path.home() / ".config" / APP_NAME.lower() / "config.json"


DATA_DIR = Path(
    os.environ.get("EXELCODE_HOME")
    or os.environ.get("AIFREE_HOME")
    or _default_home()
)
CHATS_DIR = DATA_DIR / "chats"
EXPORTS_DIR = DATA_DIR / "exports"
BACKUPS_DIR = DATA_DIR / "backups"
HISTORY_FILE = DATA_DIR / "history"
MEMORY_FILE = DATA_DIR / "memory.json"
CONFIG_FILE = Path(
    os.environ.get("EXELCODE_CONFIG")
    or os.environ.get("AIFREE_CONFIG")
    or _default_config_file()
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
    "compact_ui": "auto",  # auto | on | off (узкие терминалы/Termux)
    "proxy_mode": "off",  # off | auto | always
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
    # newline="" — писать ровно те символы, что передали: иначе Windows
    # превратит "\n" в "\r\n" и попортит файлы с CRLF (двойной \r).
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)
