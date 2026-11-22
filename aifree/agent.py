"""Agent: explores the project, edits files, and runs checks.

У анонимного Gemini нет нативного tool calling, поэтому протокол текстовый:

    READ: relative/path.py
    READ: relative/path.py:10-80
    ```file path="relative/path.py"
    ...
    ```
    ```patch path="relative/path.py"
    <<<<<<< SEARCH
    ...
    =======
    ...
    >>>>>>> REPLACE
    ```
    ```delete path="relative/path.py"
    ```
    ```run
    python3 -m pytest -q
    ```

Все пути проверяются, секреты и служебные папки запрещены, перед записью
a backup is created. Commands run only with confirmation.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR, atomic_write
from .console import (
    IS_WINDOWS,
    adapt_command,
    decode_output,
    normalize_newlines,
    utf8_child_env,
)
from .gemini import Gemini
from .render import MarkdownRenderer, Palette, compact_mode

MAX_WRITE_BYTES = 1_000_000
MAX_READ_BYTES = 2_000_000
MAX_RUN_OUTPUT = 8000
DEFAULT_RUN_TIMEOUT = 60.0
AGENT_MODES = ("ask", "auto", "dry-run")
# Имя интерпретатора в примерах команд: в Windows нет python3.
PYTHON_CMD = "python" if IS_WINDOWS else "python3"

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    "node_modules",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
    ".ssh",
    ".gnupg",
    ".cargo",
    ".rustup",
    ".npm",
    ".gradle",
    ".android",
    ".termux",
    ".local",
    "Trash",
}

IGNORED_FILES = {".DS_Store", "Thumbs.db", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

BINARY_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".class", ".jar", ".war", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".tiff", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".db", ".sqlite", ".sqlite3", ".lock",
}

SECRET_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc",
    ".pypirc", "credentials", "credentials.json", "secrets.json",
    "secret.json", "token.txt", "tokens.json",
}

SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}

CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sh", ".bash",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".cs",
    ".rb", ".php", ".sql", ".ini", ".cfg", ".conf", ".xml", ".svg",
    ".vue", ".svelte", ".kt", ".swift", ".dart", ".lua", ".pl", ".r",
}


class AgentError(RuntimeError):
    """Понятная пользователю ошибка агента."""


@dataclass
class CommandRisk:
    """Оценка опасности команды, которую предлагает агент."""

    level: str = "ok"  # ok | caution | danger
    reasons: list[str] = field(default_factory=list)

    @property
    def dangerous(self) -> bool:
        return self.level == "danger"


# Паттерны намеренно консервативные: лучше предупредить лишний раз,
# чем молча выполнить разрушительную команду в auto-режиме.
_DANGER_COMMANDS: tuple[tuple[str, str], ...] = (
    (r"(?:^|[;&|]\s*)sudo\b", "sudo может изменить систему"),
    (r"\brm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*\b", "rm -rf удаляет файлы безвозвратно"),
    (r"\brm\s+.*\s(?:/|~|\$HOME|\*|\.\.)(?:\s|$)", "rm по системному/домашнему пути"),
    (r"\b(?:mkfs(?:\.\w+)?|fdisk|parted|diskpart|shred)\b", "команда работы с диском"),
    (r"\b(?:format(?:\.com)?|shutdown|reboot|poweroff|halt)\b", "команда выключения/форматирования"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd может перезаписать устройство"),
    (r":\s*\(\s*\)\s*\{[^\n]*\|[^\n]*\}", "похоже на fork-бомбу"),
    (r"\b(?:del|erase)\b[^\n]*\s/[sq]\b", "del /s удаляет дерево файлов"),
    (r"\b(?:rd|rmdir)\b[^\n]*\s/s\b", "rd /s удаляет дерево каталогов"),
    (r"\breg\s+delete\b", "удаление ключей реестра"),
    (r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?)\b", "запуск скачанного скрипта"),
    (r"\b(?:powershell|pwsh)\b[^\n]*(?:-enc|-e\s|invoke-expression|\biex\b)", "скрытый PowerShell-код"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard теряет незакоммиченные изменения"),
    (r"\bgit\s+clean\s+-[a-z]*[fdx]", "git clean удаляет неотслеживаемые файлы"),
    (r"\bchmod\s+-R\s+777\s+/\b", "chmod 777 на весь корень"),
    (r">\s*/dev/(?:sd|nvme|disk)", "запись напрямую в устройство"),
    (r">\s*(?:/etc/|/boot/|[A-Za-z]:\\Windows\\)", "запись в системный каталог"),
)

_CAUTION_COMMANDS: tuple[tuple[str, str], ...] = (
    (r"\brm\b", "удаление файлов"),
    (r"\b(?:del|erase|rd|rmdir)\b", "удаление файлов/каталогов"),
    (r"\b(?:mv|move|rename|ren)\b", "перемещение/переименование файлов"),
    (r"\b(?:pip|pip3|npm|pnpm|yarn|apt|apt-get|brew|choco|winget)\s+(?:install|add|upgrade|update)\b", "установка пакетов"),
    (r"\b(?:curl|wget)\b", "сетевой запрос"),
    (r"\bgit\s+(?:push|reset|checkout|clean|rebase|merge)\b", "изменение git-истории/веток"),
    (r"\b(?:chmod|chown|chgrp)\b", "изменение прав доступа"),
    (r"\b(?:docker|systemctl|service|kill|pkill|taskkill)\b", "управление процессами/сервисами"),
    (r"\b(?:bash|sh|zsh)\s+-c\b", "выполнение вложенного shell-скрипта"),
)


def assess_command(cmd: str) -> CommandRisk:
    """Классифицирует команду перед запуском.

    danger — блокируется в auto-режиме, в ask требует явного подтверждения;
    caution — выполняется, но с предупреждением.
    """
    text = " ".join((cmd or "").strip().split())
    if not text:
        return CommandRisk()
    low = text.lower()
    danger_reasons: list[str] = []
    caution_reasons: list[str] = []
    for pattern, reason in _DANGER_COMMANDS:
        if re.search(pattern, low, re.I):
            danger_reasons.append(reason)
    if danger_reasons:
        return CommandRisk("danger", danger_reasons)
    for pattern, reason in _CAUTION_COMMANDS:
        if re.search(pattern, low, re.I):
            caution_reasons.append(reason)
    if caution_reasons:
        return CommandRisk("caution", caution_reasons)
    return CommandRisk()


@dataclass
class FileOp:
    kind: str
    path: str
    content: str = ""
    hunks: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class AgentPlan:
    reads: list[str] = field(default_factory=list)
    ops: list[FileOp] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    notes: str = ""
    done: bool = False
    raw: str = ""


@dataclass
class PreparedFile:
    path: str
    old: str | None
    new: str | None
    kind: str


@dataclass
class AgentResult:
    applied: bool = False
    dry_run: bool = False
    changed: list[str] = field(default_factory=list)
    ran: list[dict] = field(default_factory=list)
    backup: Path | None = None
    rounds: int = 0
    reply: str = ""
    error: str = ""
    done: bool = False
    touched_files: list[str] = field(default_factory=list)


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _is_secret(name: str) -> bool:
    low = name.lower()
    if low in (".env.example", ".env.sample", ".env.template"):
        return False
    if low == ".env" or low.startswith(".env."):
        return True
    if low in SECRET_NAMES:
        return True
    return low.endswith(tuple(SECRET_SUFFIXES))


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    return (cleaned or "workspace")[:40]


def _workspace_hash(root: Path) -> str:
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]


def normalize_agent_mode(value: str | None, default: str = "ask") -> str:
    raw = str(value or default).strip().lower().replace("_", "-")
    aliases = {
        "ask": "ask",
        "confirm": "ask",
        "спрашивать": "ask",
        "auto": "auto",
        "aut": "auto",
        "y": "auto",
        "yes": "auto",
        "авто": "auto",
        "plan": "dry-run",
        "dry": "dry-run",
        "dry-run": "dry-run",
        "dryrun": "dry-run",
        "план": "dry-run",
    }
    return aliases.get(raw, default if default in AGENT_MODES else "ask")


_READ_SPEC_RE = re.compile(
    r"^(?P<path>.+?)(?::(?P<a>\d+)?-(?P<b>\d+)?|#L(?P<a2>\d+)(?:-L?(?P<b2>\d+))?)$"
)


def parse_read_spec(spec: str) -> tuple[str, int | None, int | None]:
    spec = str(spec).strip().strip("\"'`")
    match = _READ_SPEC_RE.match(spec)
    if not match:
        return spec, None, None
    a = match.group("a") or match.group("a2")
    b = match.group("b") or match.group("b2")
    # если не похоже на диапазон (на всякий случай), считаем частью пути
    if a is None and b is None:
        return spec, None, None
    start = int(a) if a else None
    end = int(b) if b else None
    if start is not None and start < 1:
        start = 1
    if end is not None and end < 1:
        end = 1
    path = match.group("path").strip()
    if not path:
        return spec, None, None
    return path, start, end


def format_read_key(path: str, start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return path
    if start is not None and end is not None:
        return f"{path}:{start}-{end}"
    if start is not None:
        return f"{path}:{start}-"
    return f"{path}:-{end}"


def _read_bounds(start: int | None, end: int | None) -> tuple[int, float]:
    return (1 if start is None else int(start), float("inf") if end is None else int(end))


def _spec_covered(files: dict[str, str], spec: str) -> str | None:
    """Return reason string if spec is already covered by loaded context, else None."""
    path, start, end = parse_read_spec(spec)
    key = format_read_key(path, start, end)
    if key in files:
        return f"{key}: already in context"
    full = files.get(path)
    if full is not None and not full.startswith("[не удалось прочитать:"):
        return f"{path}: file already fully in context"
    # if requesting full file but only partial ranges exist, not fully covered
    if start is None and end is None:
        return None
    req_s, req_e = _read_bounds(start, end)
    for existing in files:
        try:
            epath, es, ee = parse_read_spec(existing)
        except Exception:
            continue
        if epath != path:
            continue
        if files.get(existing, "").startswith("[не удалось прочитать:"):
            continue
        if es is None and ee is None:
            return f"{path}: file already fully in context"
        ex_s, ex_e = _read_bounds(es, ee)
        if ex_s <= req_s and ex_e >= req_e:
            return f"{key}: range already in context"
    return None


def is_action_request(text: str) -> bool:
    """Эвристика: похоже ли сообщение на просьбу создать/изменить/запустить что-то."""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()

    # Явные вопросы/объяснения — обычно обычный чат, если нет явного действия "сделай сейчас".
    if re.match(
        r"^\s*(как\b|что такое|что значит|почему|зачем|объясни|расскажи|подскажи|"
        r"в ч[её]м|какая разница|можно ли|what is|what does|how to|how do i|how can i|"
        r"why|explain|tell me about|describe)\b",
        low,
    ):
        if not re.search(
            r"\b(?:и\s+)?(создай|сделай|запиши|сохрани|исправь|измени|удали|переименуй|"
            r"добавь|реализуй|запусти|create|write|save|fix|edit|delete|add|implement|run)\b"
            r".*(?:\b[\w.-]+\.(?:py|js|jsx|ts|tsx|html|css|json|ya?ml|toml|md|sh|txt|go|rs|java|c|cpp|h|rb|php|sql)\b|/)",
            low,
        ):
            return False

    has_file = bool(
        re.search(
            r"\b(файл\w*|скрипт\w*|проект\w*|модул\w*|каталог\w*|директори\w*|папк\w*|"
            r"исходник\w*|репозитори\w*)\b",
            low,
        )
    ) or bool(
        re.search(
            r"\b[\w.-]+\.(?:py|pyw|js|jsx|mjs|cjs|ts|tsx|html|htm|css|scss|sass|"
            r"json|ya?ml|toml|md|txt|sh|bash|zsh|go|rs|java|kt|c|h|cpp|hpp|cc|"
            r"cs|rb|php|sql|vue|svelte|swift|dart|lua|xml|svg|ini|cfg|conf|env)\b",
            low,
        )
    ) or bool(re.search(r"(?<!\w)(?:\./|~/|/)?[\w.-]+/[\w./-]+", t))

    has_action = bool(
        re.search(
            r"\b(создай|создать|сделай|запиши|сохрани|добавь|вставь|измени|поменяй|"
            r"поправь|исправь|почини|починить|чинить|налади|удали|убери|переименуй|"
            r"перемести|реализуй|допиши|перепиши|отредактируй|сгенерируй|обнови|"
            r"настрой|инициализируй|запусти|протестируй|выполни|собери|"
            r"create|make|write|save|add|insert|edit|modify|change|fix|patch|"
            r"delete|remove|rename|move|implement|refactor|generate|update|init|"
            r"scaffold|setup|run|test|execute|build)\b",
            low,
        )
    )

    explicit_target = bool(
        re.search(
            r"\bв\s+(?:этот\s+|текущий\s+|мо[йё]\s+)?(?:файл|проект|папк|каталог|директори|репозитори)",
            low,
        )
    ) or bool(
        re.search(
            r"\b(?:in|to|into|inside)\s+(?:the\s+)?(?:file|project|folder|directory|repo)\b",
            low,
        )
    )

    if has_action and (has_file or explicit_target):
        return True

    if re.search(
        r"\b(запусти|протестируй|выполни|собери|почини|исправь|run|test|execute|build|fix)\b.*"
        r"\b(тест\w*|команд\w*|скрипт\w*|pytest|unittest|npm|pnpm|yarn|make|cargo|go\b|python\b|pip\b|gradle|mvn)\b",
        low,
    ):
        return True
    if re.search(r"\b(почини|исправь)\b.*\b(тест\w*|ошибк\w*|failed|failing)\b", low):
        return True
    return False

    return False


_FILE_REF_RE = re.compile(
    r"\b[\w.-]+\.(?:py|pyw|js|jsx|mjs|cjs|ts|tsx|html|htm|css|scss|sass|"
    r"json|ya?ml|toml|md|txt|sh|bash|zsh|go|rs|java|kt|c|h|cpp|hpp|cc|"
    r"cs|rb|php|sql|vue|svelte|swift|dart|lua|xml|svg|ini|cfg|conf)\b",
    re.I,
)


def needs_workspace_context(text: str) -> bool:
    """Нужно ли смотреть файлы проекта, чтобы ответить на вопрос."""
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return False
    if _FILE_REF_RE.search(t):
        return True
    if re.search(r"(?<!\w)(?:\./|~/|/)?[\w.-]+/[\w./-]+", t):
        return True
    if re.search(
        r"\b(в\s+(?:этом\s+|текущем\s+|мо[её]м\s+)?(?:проект|репозитори|каталог|папк|директори|код)|"
        r"этот\s+проект|этот\s+репозитори|здесь в проект|в текущей папк|"
        r"in (?:this|the) (?:project|repo|repository|codebase|folder|directory)|"
        r"this (?:project|repo|codebase))\b",
        low,
    ):
        return True
    if re.search(
        r"\b(проект\w*|репозитори\w*|кодовая база|codebase)\b.*\b(о ч[её]м|что делает|как устроен|как работает|что внутри|структур)\b",
        low,
    ):
        return True
    if re.search(
        r"\b(о ч[её]м|что делает|как устроен|как работает|что внутри|структур)\b.*\b(проект\w*|репозитори\w*|кодовая база|codebase)\b",
        low,
    ):
        return True
    return False


_SMALL_TALK_RE = re.compile(
    r"^\s*(привет\w*|здравствуй\w*|хай|йо|ку|салют|добрый\s+\w+|доброе\s+\w+|"
    r"спасибо\w*|благодар\w*|ок(?:ей)?|ок\b|понятно|ясно|хорошо|круто|супер|"
    r"как\s+дела|как\s+ты|как\s+тебе|как\s+жизнь|как\s+настроение|"
    r"кто\s+ты|что\s+ты\s+умеешь|что\s+ты\s+можешь|"
    r"hi|hello|hey|yo|thanks|thank you|ok|okay|good\s+\w+|how are you|"
    r"who are you|what can you do|what are you)[!?.\s]*$",
    re.I,
)

_FOLLOWUP_RE = re.compile(
    r"\b(продолж\w*|дальше|давай дальше|продолжим|додел\w*|допиши\w*|дополн\w*|"
    r"ещё|еще|"
    r"continue|go on|keep going|carry on|next|resume)\b",
    re.I,
)


def _is_followup_with_context(
    task: str, history: list | None, recent_files: list[str] | None
) -> bool:
    """Follow-up like 'continue' should use agent context when available."""
    if not (history or recent_files):
        return False
    t = (task or "").strip()
    if not t:
        return False
    if _FOLLOWUP_RE.search(t):
        return True
    # short ambiguous request with known recent files -> prefer full flow with context
    if recent_files and len(t.split()) <= 10 and not _SMALL_TALK_RE.match(t):
        return True
    return False


def should_answer_without_scan(text: str) -> bool:
    """Можно ли ответить без сканирования файлов проекта."""
    t = (text or "").strip()
    if not t:
        return True
    if is_action_request(t):
        return False
    if needs_workspace_context(t):
        return False
    low = t.lower()
    if _SMALL_TALK_RE.match(t):
        return True
    # общий вопрос без просьбы исследовать проект/файлы
    if "?" in t:
        if re.search(
            r"\b(посмотри|покажи|найди|проверь|открой|прочитай|изучи|разберись|"
            r"проанализируй|в проект|в код|в файл|в репозитор|в папк|в каталог|"
            r"look|show|find|check|open|read|inspect|analyze|search|"
            r"in (?:the )?(?:project|repo|code|file|folder|directory))\b",
            low,
        ):
            return False
        return True
    # без вопроса и без явного запроса к проекту — лучше не сканировать вслепую
    if re.search(
        r"\b(посмотри|покажи|найди|проверь|открой|прочитай|изучи|разберись|проанализируй)\b",
        low,
    ):
        return False
    return True


class Workspace:
    """Безопасный доступ к папке проекта."""

    def __init__(self, root: str | Path, max_file_bytes: int = 200_000):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise AgentError(f"папка не найдена: {self.root}")
        self.max_file_bytes = max_file_bytes

    def resolve(self, rel: str, must_exist: bool = False) -> Path:
        raw = str(rel).strip().strip("\"'`")
        if not raw:
            raise AgentError("empty path")
        if "\x00" in raw:
            raise AgentError("invalid path")
        if any(ch in raw for ch in "\"'=<>") or any(
            ord(ch) < 32 or ord(ch) == 127 for ch in raw
        ):
            raise AgentError(f"invalid characters in path: {raw!r}")
        path = Path(raw)
        if not path.parts:
            raise AgentError("empty path")
        if path.is_absolute():
            raise AgentError(f"absolute path is not allowed: {raw}")
        if any(part in ("..", "~") for part in path.parts):
            raise AgentError(f"path traversal is not allowed: {raw}")
        if any(part in IGNORED_DIRS for part in path.parts):
            raise AgentError(f"service directory is not allowed: {raw}")
        if _is_secret(path.name):
            raise AgentError(f"secret file is not allowed: {raw}")
        target = (self.root / path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise AgentError(f"path outside workspace: {raw}") from exc
        raw_path = self.root / path
        if raw_path.is_symlink():
            raise AgentError(f"symbolic links are not allowed: {raw}")
        if must_exist and not target.is_file():
            raise AgentError(f"file not found: {raw}")
        return target

    def read_text(
        self, rel: str, start: int | None = None, end: int | None = None
    ) -> str:
        base, s2, e2 = parse_read_spec(rel)
        if start is None:
            start = s2
        if end is None:
            end = e2
        path = self.resolve(base, must_exist=True)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AgentError(f"{base}: {exc}") from exc
        if start is None and end is None:
            if size > self.max_file_bytes:
                raise AgentError(
                    f"{base}: файл слишком большой ({_human_size(size)}), "
                    "запроси диапазон строк, например READ: "
                    f"{base}:1-120"
                )
        elif size > MAX_READ_BYTES:
            raise AgentError(
                f"{base}: файл слишком большой для чтения фрагмента ({_human_size(size)})"
            )
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            raise AgentError(f"{base}: бинарный файл")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", "replace")

        # Модель, diff и патчи работают с LF: файлы, сохранённые на Windows
        # с CRLF, иначе не совпадут с SEARCH из ответа модели.
        text = normalize_newlines(text)

        if start is None and end is None:
            return text

        lines = text.splitlines(keepends=True)
        total = len(lines)
        s = 1 if start is None else max(1, start)
        e = total if end is None else min(total, end)
        if total == 0:
            return "[файл пустой]"
        if s > total:
            raise AgentError(f"{base}: строка {s} вне диапазона (всего {total})")
        if e < s:
            raise AgentError(f"{base}: неверный диапазон {s}-{e}")
        return "".join(lines[s - 1 : e])

    def tree(self, limit: int = 400) -> str:
        lines: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for name in sorted(filenames):
                if name in IGNORED_FILES or _is_secret(name):
                    continue
                path = Path(dirpath) / name
                if path.suffix.lower() in BINARY_SUFFIXES:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > self.max_file_bytes:
                    continue
                rel = path.relative_to(self.root).as_posix()
                lines.append(f"{rel} ({_human_size(size)})")
                if len(lines) >= limit:
                    lines.append("... (список обрезан)")
                    return "\n".join(lines)
        return "\n".join(lines) if lines else "(пусто)"


_FENCE_OPEN_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*(.*)$")
_HINT_PATH_RE = re.compile(r"^([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)$")


def _clean_path(value: str) -> str:
    value = (value or "").strip().strip("`").strip()
    # tolerate malformed quoted values, including missing closing quote
    m = re.match(r'^["\'](.*)$', value)
    if m:
        value = m.group(1)
        if value and value[-1] in "\"'":
            value = value[:-1]
    else:
        # unquoted: take first token
        if value:
            value = value.split()[0]
    value = value.strip().strip("\"'`")
    if value.startswith("./"):
        value = value[2:]
    return value.strip()


def _extract_path_value(rest: str) -> str | None:
    rest = (rest or "").strip()
    if not rest:
        return None
    # accept common mistaken keys: path/file/name/patch="..."
    m = re.match(r'(?i)^(?:path|file|name|patch)\s*[:=]\s*(.+)$', rest)
    if m:
        rest = m.group(1).strip()
    if not rest:
        return None
    m = re.match(r'^"([^"\n]*?)"?\s*$', rest)
    if m:
        return m.group(1).strip()
    m = re.match(r"^'([^'\n]*?)'?\s*$", rest)
    if m:
        return m.group(1).strip()
    token = rest.split()[0] if rest else ""
    token = token.strip().strip("\"'`")
    return token.strip() or None


def _looks_like_patch_body(body: str) -> bool:
    return bool(
        re.search(r"^<<<<<<< ", body, re.M)
        and re.search(r"^=======", body, re.M)
        and re.search(r"^>>>>>>> ", body, re.M)
    )


def _parse_info(info: str) -> tuple[str, str] | None:
    if not info:
        return None
    match = re.search(
        r"(?i)\b(file|write|patch|replace|delete|remove|read|run|exec|command|cmd|bash|sh|shell)\b"
        r"\s*(?:path\s*)?[:=]?\s*(.*)$",
        info,
    )
    if not match:
        return None
    kind = match.group(1).lower()
    rest = match.group(2).strip()
    if kind in ("run", "exec", "command", "cmd", "bash", "sh", "shell"):
        # for run, path/command in header is optional; body usually contains command
        path = _extract_path_value(rest) or ""
        return "run", path
    path = _extract_path_value(rest)
    if not path:
        return None
    if kind in ("read",):
        return "read", path
    return kind, path


def _path_from_hint(before: str) -> str | None:
    lines = [line.strip() for line in before.splitlines() if line.strip()]
    if not lines:
        return None
    last = re.sub(r"^#{1,6}\s*", "", lines[-1]).strip().strip("*`_ ")
    match = re.match(r"(?i)^(?:file|файл|path|путь)\s*[:=]?\s+(.+)$", last)
    candidate = match.group(1) if match else last
    candidate = candidate.strip("*`_ \"'")
    if not _HINT_PATH_RE.match(candidate):
        return None
    if Path(candidate).suffix.lower() in CODE_SUFFIXES or "/" in candidate:
        return candidate
    return None


def _iter_blocks(text: str) -> tuple[str, list[tuple[str, str, bool, str]]]:
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[str, str, bool, str]] = []
    outside: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        match = _FENCE_OPEN_RE.match(raw.rstrip("\r\n"))
        if not match:
            outside.append(raw)
            index += 1
            continue
        marker = match.group(2)
        info = match.group(3).strip()
        char = marker[0]
        length = len(marker)
        before = "".join(outside)
        body: list[str] = []
        index += 1
        closed = False
        while index < len(lines):
            line = lines[index]
            if re.match(
                rf"^\s*{re.escape(char)}{{{length},}}\s*$", line.rstrip("\r\n")
            ):
                closed = True
                index += 1
                break
            body.append(line)
            index += 1
        blocks.append((info, "".join(body), closed, before))
    return "".join(outside), blocks


def parse_patch(body: str) -> list[tuple[str, str]]:
    lines = body.splitlines(keepends=True)
    hunks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("<<<<<<<"):
            index += 1
            continue
        index += 1
        search: list[str] = []
        while index < len(lines) and not lines[index].lstrip().startswith("======="):
            search.append(lines[index])
            index += 1
        if index >= len(lines):
            raise AgentError("в блоке patch нет разделителя =======")
        index += 1
        replace: list[str] = []
        while index < len(lines) and not lines[index].lstrip().startswith(">>>>>>>"):
            replace.append(lines[index])
            index += 1
        if index >= len(lines):
            raise AgentError("в блоке patch нет закрывающего >>>>>>>")
        index += 1
        hunks.append(("".join(search), "".join(replace)))
    if not hunks:
        raise AgentError("в блоке patch нет пар SEARCH/REPLACE")
    return hunks


def parse_agent_response(text: str) -> AgentPlan:
    outside, blocks = _iter_blocks(text)
    reads: list[str] = []
    ops: list[FileOp] = []
    runs: list[str] = []
    notes: list[str] = []
    done = False

    for line in outside.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(
            r"(?i)^(?:READ|ЧИТАЙ|ОТКРОЙ|ПРОЧИТАЙ)\s*[:=]\s*(.+)$", stripped
        )
        if match:
            reads.append(_clean_path(match.group(1)))
            continue
        match = re.match(
            r"(?i)^(?:DELETE|УДАЛИ|УДАЛИТЬ)\s*[:=]\s*(.+)$", stripped
        )
        if match:
            ops.append(FileOp("delete", _clean_path(match.group(1))))
            continue
        match = re.match(
            r"(?i)^(?:RUN|EXEC|ЗАПУСТИ|ВЫПОЛНИ)\s*[:=]\s*(.+)$", stripped
        )
        if match:
            cmd = match.group(1).strip()
            if cmd:
                runs.append(cmd)
            continue
        if re.fullmatch(r"(?i)(?:DONE|ГОТОВО|ГОТОВ)\s*[.!]*", stripped):
            done = True
            continue
        notes.append(stripped)

    for info, body, closed, before in blocks:
        parsed = _parse_info(info)
        if not parsed:
            hint = _path_from_hint(before)
            if hint:
                parsed = ("patch" if "<<<<<<<" in body else "write", hint)
        if not parsed:
            continue
        kind, path = parsed
        label = path or "run"
        if not closed:
            notes.append(f"[блок {label} не закрыт и пропущен]")
            continue
        if kind == "read":
            for line in body.splitlines():
                value = re.sub(
                    r"(?i)^(?:READ|ЧИТАЙ|ОТКРОЙ|ПРОЧИТАЙ)\s*[:=]\s*", "", line.strip()
                )
                if value:
                    reads.append(_clean_path(value))
        elif kind == "run":
            cmd = body.strip()
            if cmd:
                runs.append(cmd)
        elif kind in ("file", "write"):
            if _looks_like_patch_body(body):
                # model sometimes puts SEARCH/REPLACE inside a file/write block
                ops.append(FileOp("patch", path, hunks=parse_patch(body)))
            else:
                ops.append(FileOp("write", path, content=body))
        elif kind in ("patch", "replace"):
            ops.append(FileOp("patch", path, hunks=parse_patch(body)))
        elif kind in ("delete", "remove"):
            ops.append(FileOp("delete", path))

    seen: set[str] = set()
    unique_reads: list[str] = []
    for rel in reads:
        if rel and rel not in seen:
            seen.add(rel)
            unique_reads.append(rel)

    unique_runs: list[str] = []
    seen_runs: set[str] = set()
    for cmd in runs:
        key = cmd.strip()
        if key and key not in seen_runs:
            seen_runs.add(key)
            unique_runs.append(key)

    return AgentPlan(
        reads=unique_reads,
        ops=ops,
        runs=unique_runs,
        notes="\n".join(notes).strip(),
        done=done,
        raw=text,
    )


def _search_variants(search: str) -> list[str]:
    variants: list[str] = []
    if search.endswith("\r\n"):
        variants.append(search[:-2])
    elif search.endswith("\n"):
        variants.append(search[:-1])
    else:
        variants.append(search + "\n")
    variants.append(search.replace("\n", "\r\n"))
    variants.append(search.rstrip())
    return variants


def apply_hunks(text: str, hunks: list[tuple[str, str]], rel: str) -> str:
    for number, (search, replace) in enumerate(hunks, 1):
        if not search:
            raise AgentError(f"{rel}: пустой SEARCH в патче №{number}")
        candidate = search
        count = text.count(candidate)
        if count == 0:
            for variant in _search_variants(search):
                if variant == search:
                    continue
                if text.count(variant) == 1:
                    candidate = variant
                    count = 1
                    break
        if count == 0:
            first = search.splitlines()[0] if search.splitlines() else ""
            raise AgentError(
                f"{rel}: SEARCH не найден (начало: {first[:80]!r})"
            )
        if count > 1:
            raise AgentError(
                f"{rel}: SEARCH встречается {count} раз, нужен уникальный фрагмент"
            )
        new_piece = replace
        if "\r\n" in text and "\r\n" not in new_piece:
            new_piece = new_piece.replace("\n", "\r\n")
        text = text.replace(candidate, new_piece, 1)
    return text


def prepare_ops(workspace: Workspace, ops: list[FileOp]) -> list[PreparedFile]:
    original: dict[str, str | None] = {}
    virtual: dict[str, str | None] = {}
    order: list[str] = []

    for op in ops:
        path = workspace.resolve(op.path)
        rel = path.relative_to(workspace.root).as_posix()
        if rel not in virtual:
            if path.exists():
                if not path.is_file():
                    raise AgentError(f"{rel}: это не файл")
                old = workspace.read_text(rel)
            else:
                old = None
            original[rel] = old
            virtual[rel] = old
            order.append(rel)

        current = virtual[rel]
        if op.kind == "write":
            new: str | None = op.content
        elif op.kind == "patch":
            if current is None:
                raise AgentError(f"{rel}: нельзя патчить несуществующий файл")
            new = apply_hunks(current, op.hunks, rel)
        elif op.kind == "delete":
            if current is None:
                raise AgentError(f"{rel}: файл не существует")
            new = None
        else:
            raise AgentError(f"неизвестная операция: {op.kind}")

        if new is not None and len(new.encode("utf-8")) > MAX_WRITE_BYTES:
            raise AgentError(f"{rel}: результат слишком большой")
        virtual[rel] = new

    prepared: list[PreparedFile] = []
    for rel in order:
        old = original[rel]
        new = virtual[rel]
        if old == new:
            continue
        kind = "create" if old is None else ("delete" if new is None else "update")
        prepared.append(PreparedFile(rel, old, new, kind))
    return prepared


def diff_for(item: PreparedFile) -> list[str]:
    old = item.old or ""
    new = item.new or ""
    fromfile = "/dev/null" if item.old is None else f"a/{item.path}"
    tofile = "/dev/null" if item.new is None else f"b/{item.path}"
    return list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            n=3,
        )
    )


def detect_newline(path: Path) -> str:
    """Перевод строки файла: CRLF (Windows) или LF."""
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(65536)
    except OSError:
        return "\n"
    return "\r\n" if b"\r\n" in chunk else "\n"


def with_newline(text: str, newline: str) -> str:
    """Приводит текст к LF и расставляет нужный перевод строки."""
    text = normalize_newlines(text)
    if newline == "\n":
        return text
    return text.replace("\n", newline)


def apply_prepared(
    workspace: Workspace,
    prepared: list[PreparedFile],
    backup_root: Path | None = None,
    task: str = "",
) -> Path:
    backup_root = Path(backup_root or (DATA_DIR / "backups"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = (
        backup_root
        / f"{_safe_name(workspace.root.name)}-{_workspace_hash(workspace.root)}"
        / f"{stamp}-{uuid.uuid4().hex[:4]}"
    )
    files_dir = run_dir / "files"
    manifest = {
        "workspace": str(workspace.root),
        "created_at": time.time(),
        "task": task[:1000],
        "status": "applying",
        "files": [],
    }

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        for item in prepared:
            if item.old is not None:
                source = workspace.root / item.path
                backup_file = files_dir / item.path
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup_file)
            manifest["files"].append(
                {"path": item.path, "existed": item.old is not None, "kind": item.kind}
            )
        atomic_write(
            run_dir / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        for item in prepared:
            target = workspace.root / item.path
            if item.new is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            else:
                # Сохраняем исходный перевод строки файла, чтобы патч не
                # превращал весь CRLF-файл в LF.
                newline = detect_newline(target) if item.old is not None else "\n"
                atomic_write(target, with_newline(item.new, newline))
        manifest["status"] = "applied"
        manifest["applied_at"] = time.time()
        atomic_write(
            run_dir / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    except Exception as exc:  # noqa: BLE001 — фиксируем сбой и откатываемся
        manifest["status"] = "failed"
        manifest["error"] = str(exc)[:1000]
        try:
            atomic_write(
                run_dir / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        except OSError:
            pass
        raise AgentError(f"не удалось применить изменения: {exc}") from exc
    return run_dir


def list_backups(
    workspace: Workspace | str | Path,
    backup_root: Path | None = None,
) -> list[dict]:
    ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    backup_root = Path(backup_root or (DATA_DIR / "backups"))
    base = backup_root / f"{_safe_name(ws.root.name)}-{_workspace_hash(ws.root)}"
    items: list[dict] = []
    if not base.is_dir():
        return items
    for manifest_path in base.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data["_path"] = str(manifest_path.parent)
        items.append(data)
    items.sort(key=lambda d: float(d.get("created_at", 0) or 0), reverse=True)
    return items


def undo_last(
    workspace: Workspace | str | Path,
    backup_root: Path | None = None,
) -> dict | None:
    ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    for data in list_backups(ws, backup_root):
        if data.get("status") not in ("applied", "failed"):
            continue
        run_dir = Path(data.pop("_path"))
        restored: list[str] = []
        for entry in data.get("files", []):
            rel = entry.get("path", "")
            if not rel:
                continue
            target = ws.root / rel
            if entry.get("existed"):
                source = run_dir / "files" / rel
                if not source.is_file():
                    raise AgentError(f"бэкап повреждён: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                if target.is_file():
                    target.unlink()
            restored.append(rel)
        data["status"] = "undone"
        data["undone_at"] = time.time()
        atomic_write(
            run_dir / "manifest.json",
            json.dumps(data, ensure_ascii=False, indent=2),
        )
        data["restored"] = restored
        data["_path"] = str(run_dir)
        return data
    return None


def execute_command(
    cmd: str,
    cwd: str | Path,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    max_chars: int = MAX_RUN_OUTPUT,
) -> tuple[int, str, float]:
    """Запускает команду в cwd. Возвращает (код, вывод, длительность)."""
    cmd = adapt_command(cmd)
    cwd = Path(cwd)
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            env=utf8_child_env(),
        )
        output = decode_output(proc.stdout or b"")
        if proc.stderr:
            err = decode_output(proc.stderr)
            if output and not output.endswith("\n"):
                output += "\n"
            output += err
        code = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        output = decode_output(exc.stdout or b"") + decode_output(exc.stderr or b"")
        if output and not output.endswith("\n"):
            output += "\n"
        output += f"[timeout {timeout:g}s]"
        code = 124
    except Exception as exc:  # noqa: BLE001
        output = f"[launch error: {exc}]"
        code = 1
    elapsed = time.time() - started
    output = output.replace("\r\n", "\n")
    if len(output) > max_chars:
        output = output[: max(0, max_chars - 40)] + "\n… (вывод обрезан)"
    return code, output, elapsed


def format_run_feedback(cmd: str, code: int, output: str, elapsed: float) -> str:
    status = "ok" if code == 0 else f"exit {code}"
    body = output if output.strip() else "(пустой вывод)"
    return (
        f"<run command={cmd!r} status={status!r} elapsed=\"{elapsed:.1f}s\">\n"
        f"{body}\n</run>"
    )


def _format_files(files: dict[str, str], limit: int) -> str:
    if not files:
        return "(no files read yet)"
    parts: list[str] = []
    used = 0
    for rel, content in files.items():
        block = f"--- {rel} ---\n{content}\n"
        if used + len(block) > limit:
            parts.append(f"--- {rel} ---\n[omitted: context limit]\n")
            continue
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _format_history(messages: list | None, limit: int = 5) -> str:
    if not messages:
        return "(no recent chat history)"
    items = list(messages)[-max(1, limit):]
    parts: list[str] = []
    for msg in items:
        if isinstance(msg, dict):
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", ""))
        else:
            role = str(getattr(msg, "role", "user"))
            content = str(getattr(msg, "content", ""))
        who = "user" if role == "user" else "assistant"
        text = content.strip()
        if len(text) > 1200:
            text = text[:1200] + "\n…"
        if not text:
            continue
        parts.append(f"{who}: {text}")
    return "\n\n".join(parts) if parts else "(no recent chat history)"


def _format_recent_files(files: list[str] | None) -> str:
    if not files:
        return "(none)"
    # keep unique, preserve recent-first/last order reasonably
    seen: set[str] = set()
    ordered: list[str] = []
    for f in files:
        if not f or f in seen:
            continue
        seen.add(f)
        ordered.append(f)
    return "\n".join(f"- {f}" for f in ordered[-20:]) or "(none)"


def build_research_prompt(
    task: str,
    tree: str,
    files: dict[str, str],
    context_chars: int = 30000,
    history: list | None = None,
    recent_files: list[str] | None = None,
) -> str:
    budget = max(4000, context_chars - len(tree) - 3000)
    hist = _format_history(history, limit=5)
    recent = _format_recent_files(recent_files)
    return f"""You are ExelCode, an autonomous coding agent.
You work in a local project directory. User task:
<task>
{task}
</task>

Recent chat history (for follow-up context):
<history>
{hist}
</history>

Recently read/changed files in this session:
<recent_files>
{recent}
</recent_files>

File tree (relative paths):
<tree>
{tree}
</tree>

Already read files:
<files>
{_format_files(files, budget)}
</files>

PHASE 1 — research. Do not modify files.
If you need to inspect files, output up to 8 lines in this exact form:
READ: relative/path.py
For a large file you can request a range:
READ: relative/path.py:1-120
If context is sufficient, output exactly one line:
READY
Use the recent chat history and recent files to understand follow-ups like "continue".
Do not output anything else and do not propose changes in this phase.
"""


def build_edit_prompt(
    task: str,
    tree: str,
    files: dict[str, str],
    applied: list[str],
    feedback: str = "",
    context_chars: int = 30000,
    history: list | None = None,
    recent_files: list[str] | None = None,
) -> str:
    budget = max(4000, context_chars - len(tree) - 5000)
    applied_text = "\n".join(f"- {path}" for path in applied) or "(none yet)"
    feedback_text = f"\n<feedback>\n{feedback}\n</feedback>\n" if feedback else ""
    hist = _format_history(history, limit=5)
    recent = _format_recent_files(recent_files)
    return f"""You are ExelCode, an autonomous coding agent.
You work in a local project directory. User task:
<task>
{task}
</task>

Recent chat history (for follow-up context):
<history>
{hist}
</history>

Recently read/changed files in this session:
<recent_files>
{recent}
</recent_files>

File tree (relative paths):
<tree>
{tree}
</tree>

Read files:
<files>
{_format_files(files, budget)}
</files>

Already applied files:
{applied_text}
{feedback_text}
PHASE 2 — changes. Output file blocks strictly in Markdown format.

Create or fully overwrite a file:
```file path="relative/path.py"
<full file content>
```

Modify part of a file:
```patch path="relative/path.py"
<<<<<<< SEARCH
exact old fragment
=======
new fragment
>>>>>>> REPLACE
```

Delete a file:
```delete path="relative/path.py"
```

Request reading a file or range (if more context is needed):
READ: relative/path.py
READ: relative/path.py:1-120

Run a command to verify (tests, linter, run):
```run
{PYTHON_CMD} -m pytest -q
```

Rules:
- only relative paths inside the project; absolute, .., ~, .git, .env, keys and binary files are forbidden;
- SEARCH must exactly match the current file and appear exactly once;
- preserve project style, indentation, and language;
- you may output multiple blocks;
- do not repeat already applied changes without reason;
- if checks are needed after applying, output a run block; after execution you will get output and exit code;
- if a command fails, fix the cause and verify again;
- if there is nothing left to do, output DONE;
- use recent chat history and recent files to resolve follow-ups like "continue the book".
- do not wrap the entire response in an extra code block.
"""


def default_workspace(cfg: dict) -> Path:
    raw = str(cfg.get("agent_workspace") or "").strip()
    path = Path(raw).expanduser() if raw else Path.cwd()
    return path.resolve()


class Agent:
    """Agent: research, edits, checks, and fix iterations."""

    def __init__(
        self,
        workspace: Workspace | str | Path,
        model: str | dict = "auto",
        timeout: float = 60,
        context_chars: int = 30000,
        max_rounds: int = 6,
        max_edit_rounds: int = 4,
        max_fix_rounds: int = 4,
        auto_approve: bool = False,
        dry_run: bool = False,
        pal: Palette | None = None,
        out=None,
        confirm_cb=None,
        confirm_run_cb=None,
        client=None,
        backup_root: Path | None = None,
        max_file_bytes: int = 200_000,
        max_diff_lines: int = 120,
        run_timeout: float = DEFAULT_RUN_TIMEOUT,
        run_output_chars: int = MAX_RUN_OUTPUT,
        runner=None,
        quiet: bool = False,
        history: list | None = None,
        recent_files: list[str] | None = None,
        compact: bool | None = None,
    ):
        self.ws = (
            workspace
            if isinstance(workspace, Workspace)
            else Workspace(workspace, max_file_bytes=max_file_bytes)
        )
        self.model = model
        self.timeout = timeout
        self.context_chars = context_chars
        self.max_rounds = max(1, max_rounds)
        self.max_edit_rounds = max(1, max_edit_rounds)
        self.max_fix_rounds = max(0, max_fix_rounds)
        self.auto_approve = auto_approve
        self.dry_run = dry_run
        self.pal = pal or Palette(False)
        self.out = out or sys.stdout
        self.confirm_cb = confirm_cb
        self.confirm_run_cb = confirm_run_cb or confirm_cb
        self.backup_root = backup_root or (DATA_DIR / "backups")
        self.max_diff_lines = max_diff_lines
        self.run_timeout = run_timeout
        self.run_output_chars = run_output_chars
        self.runner = runner or execute_command
        self.quiet = quiet
        self.compact = compact_mode(force=compact)
        self.history = list(history or [])
        self.recent_files = list(recent_files or [])
        self.client = client or Gemini(model, timeout=timeout)

    # ── ввод-вывод ──────────────────────────────────────────────────────
    def _print(self, text: str = "") -> None:
        if not self.quiet:
            print(text, file=self.out)

    def _spinner(self, label: str = "думаю..."):
        if self.quiet or not getattr(self.out, "isatty", lambda: False)():
            return lambda: None
        stop = threading.Event()
        frames = "◐◓◑◒"
        echo_state = None

        def _suppress():
            nonlocal echo_state
            try:
                if os.name == "nt":
                    import ctypes, ctypes.wintypes as wt
                    kernel32 = ctypes.windll.kernel32
                    h = kernel32.GetStdHandle(-10)
                    mode = wt.DWORD()
                    if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                        return
                    echo_state = ("nt", h, mode.value)
                    kernel32.SetConsoleMode(h, mode.value & ~0x0004 & ~0x0002)
                else:
                    import termios, sys as _sys
                    fd = _sys.stdin.fileno()
                    old = termios.tcgetattr(fd)
                    echo_state = ("posix", fd, old)
                    new = termios.tcgetattr(fd)
                    new[3] = new[3] & ~termios.ECHO & ~termios.ICANON
                    termios.tcsetattr(fd, termios.TCSADRAIN, new)
            except Exception:
                echo_state = None

        def _restore():
            nonlocal echo_state
            if not echo_state:
                return
            try:
                if echo_state[0] == "nt":
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    _, h, old_mode = echo_state
                    kernel32.SetConsoleMode(h, old_mode)
                    kernel32.FlushConsoleInputBuffer(h)
                else:
                    import termios
                    _, fd, old = echo_state
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    try:
                        termios.tcflush(fd, termios.TCIFLUSH)
                    except Exception:
                        pass
            except Exception:
                pass
            echo_state = None

        _suppress()

        def run():
            i = 0
            while not stop.wait(0.12):
                try:
                    self.out.write("\r\033[K" + self.pal.dim(f"{frames[i % len(frames)]} {label}"))
                    self.out.flush()
                except Exception:
                    pass
                i += 1

        t = threading.Thread(target=run, name="exelcode-agent-spinner", daemon=True)
        t.start()

        def finish():
            stop.set()
            t.join(timeout=0.5)
            try:
                self.out.write("\r\033[K")
                self.out.flush()
            except Exception:
                pass
            _restore()

        return finish

    def _ask(self, prompt: str, label: str) -> str:
        self._print(self.pal.dim(f"  sys  · {label}"))
        self.out.flush()
        reply = self.client.ask(prompt)
        text = reply.text if hasattr(reply, "text") else str(reply)
        model_name = getattr(reply, "model", "?")
        elapsed = getattr(reply, "elapsed", 0.0)
        self._print(self.pal.dim(f"    {model_name} · {elapsed:.1f}s"))
        return text

    def show_plan(self, prepared: list[PreparedFile]) -> None:
        if self.quiet:
            return
        icons = {"create": "✚", "update": "✎", "delete": "✖"}
        colors = {
            "create": self.pal.green,
            "update": self.pal.yellow,
            "delete": self.pal.red,
        }
        print(self.pal.bold("\n  agent· plan"), file=self.out)
        for item in prepared:
            icon = colors[item.kind](icons[item.kind])
            print(f"  {icon} {item.kind:<6} {item.path}", file=self.out)
        print(file=self.out)
        diff_limit = min(self.max_diff_lines, 36) if self.compact else self.max_diff_lines
        for item in prepared:
            lines = diff_for(item)
            if not lines:
                continue
            print(self.pal.bold(f"  {item.path}"), file=self.out)
            shown = 0
            for line in lines:
                if shown >= diff_limit:
                    print(self.pal.dim("  … diff truncated"), file=self.out)
                    break
                line = line.rstrip("\n")
                if line.startswith(("+++", "---")):
                    print(self.pal.dim("  " + line), file=self.out)
                elif line.startswith("+"):
                    print(self.pal.green("  " + line), file=self.out)
                elif line.startswith("-"):
                    print(self.pal.red("  " + line), file=self.out)
                elif line.startswith("@@"):
                    print(self.pal.cyan("  " + line), file=self.out)
                else:
                    print("  " + line, file=self.out)
                shown += 1
            print(file=self.out)

    def _approve(self, question: str = "  применить изменения?") -> bool:
        if self.auto_approve:
            return True
        cb = self.confirm_cb
        if cb is not None:
            try:
                return bool(cb(question))
            except (EOFError, KeyboardInterrupt):
                return False
        if not sys.stdin.isatty():
            return False
        try:
            answer = input(question + " (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes", "д", "да")

    def _approve_run(self, cmd: str, risk: CommandRisk | None = None) -> bool:
        if self.auto_approve:
            return True
        short = cmd if len(cmd) <= 140 else cmd[:137] + "…"
        if risk is not None and risk.dangerous:
            question = f"  выполнить ОПАСНУЮ команду: {short}?"
        else:
            question = f"  выполнить команду: {short}?"
        cb = self.confirm_run_cb
        if cb is not None:
            try:
                return bool(cb(question))
            except (EOFError, KeyboardInterrupt):
                return False
        if not sys.stdin.isatty():
            return False
        try:
            answer = input(question + " (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes", "д", "да")

    # ── чтение и запуск ────────────────────────────────────────────────
    def _read_into(
        self, files: dict[str, str], spec: str, redundant: list[str] | None = None
    ) -> bool:
        path, start, end = parse_read_spec(spec)
        key = format_read_key(path, start, end)
        covered = _spec_covered(files, spec)
        if covered:
            if redundant is not None:
                redundant.append(covered)
            self._print(self.pal.dim(f"    ↺ {key} (already in context)"))
            return False
        try:
            content = self.ws.read_text(path, start, end)
            files[key] = content
            self._print(self.pal.dim(f"    📖 {key}"))
        except AgentError as exc:
            files[key] = f"[не удалось прочитать: {exc}]"
            self._print(self.pal.yellow(f"    ⚠ {key}: {exc}"))
        return True

    def _do_runs(self, runs: list[str]) -> tuple[list[dict], str, bool]:
        records: list[dict] = []
        feedback_parts: list[str] = []
        failed = False
        for cmd in runs:
            self._print(self.pal.cyan(f"  $ {cmd}"))
            risk = assess_command(cmd)
            for reason in risk.reasons:
                self._print(self.pal.yellow(f"    ⚠ {reason}"))
            if self.dry_run:
                self._print(self.pal.yellow("    dry-run: command not executed"))
                feedback_parts.append(
                    f"<run command={cmd!r} status='skipped' elapsed='0.0s'>\n"
                    "[dry-run: command not executed]\n</run>"
                )
                records.append(
                    {"cmd": cmd, "code": None, "output": "", "elapsed": 0.0, "skipped": True}
                )
                continue
            if risk.dangerous and self.auto_approve:
                self._print(self.pal.red(
                    "    ⛔ заблокировано политикой безопасности (опасная команда)"
                ))
                feedback_parts.append(
                    f"<run command={cmd!r} status='blocked' elapsed='0.0s'>\n"
                    "[blocked by safety policy: dangerous command. "
                    "Propose a safer alternative or ask the user to run it manually.]\n</run>"
                )
                records.append(
                    {
                        "cmd": cmd,
                        "code": None,
                        "output": "",
                        "elapsed": 0.0,
                        "skipped": True,
                        "blocked": True,
                    }
                )
                failed = True
                continue
            if not self._approve_run(cmd, risk):
                self._print(self.pal.dim("    skipped"))
                feedback_parts.append(
                    f"<run command={cmd!r} status='skipped' elapsed='0.0s'>\n"
                    "[user cancelled execution]\n</run>"
                )
                records.append(
                    {"cmd": cmd, "code": None, "output": "", "elapsed": 0.0, "skipped": True}
                )
                continue
            code, output, elapsed = self.runner(
                cmd, self.ws.root, self.run_timeout, self.run_output_chars
            )
            record = {
                "cmd": cmd,
                "code": code,
                "output": output,
                "elapsed": round(elapsed, 2),
                "skipped": False,
            }
            records.append(record)
            if output and not self.quiet:
                for line in output.splitlines()[:40]:
                    color = self.pal.red if code != 0 else self.pal.dim
                    print(color("    " + line), file=self.out)
                if output.count("\n") >= 40:
                    print(self.pal.dim("    …"), file=self.out)
            status = "ok" if code == 0 else f"exit {code}"
            mark = self.pal.green("✓") if code == 0 else self.pal.red("✗")
            self._print(f"  sys  · run     {mark} {status} · {elapsed:.1f}s")
            feedback_parts.append(format_run_feedback(cmd, code, output, elapsed))
            if code != 0:
                failed = True
        return records, "\n\n".join(feedback_parts), failed

    # ── основной цикл ──────────────────────────────────────────────────
    def run(self, task: str) -> AgentResult:
        task = task.strip()
        if not task:
            raise AgentError("пустая задача")

        def _touched(files_dict, applied_list, extra=None):
            out, seen = [], set()
            for key in list(files_dict.keys()) + list(applied_list) + list(extra or []):
                base, _, _ = parse_read_spec(str(key))
                if base and base not in seen:
                    seen.add(base)
                    out.append(base)
            return out

        # Обычный вопрос/разговор без привязки к файлам проекта — без сканирования
        has_agent_ctx = bool(self.history or self.recent_files)
        if should_answer_without_scan(task) and not _is_followup_with_context(
            task, self.history, self.recent_files
        ):
            ctx = ""
            if has_agent_ctx:
                ctx = (
                    "\n\nRecent chat history:\n"
                    f"{_format_history(self.history, limit=5)}\n\n"
                    "Recently read/changed files:\n"
                    f"{_format_recent_files(self.recent_files)}\n"
                )
            prompt = (
                "Ты полезный ассистент. Ответь на русском, кратко и по делу. "
                "If asked about your capabilities, say you are ExelCode: you can "
                "chat, and in agent mode create/edit files and run "
                "команды в выбранной папке проекта."
                f"{ctx}\n"
                f"Вопрос: {task}"
            )
            stop_spinner = self._spinner("думаю...")
            try:
                reply = self.client.ask(prompt)
                text = reply.text if hasattr(reply, "text") else str(reply)
                model_name = getattr(reply, "model", "?")
                elapsed = getattr(reply, "elapsed", 0.0)
            except Exception as exc:  # noqa: BLE001
                stop_spinner()
                raise AgentError(f"не удалось получить ответ: {exc}") from exc
            stop_spinner()

            if not self.quiet:
                self._print()
                rendered = MarkdownRenderer(
                    color=self.pal.enabled, compact=self.compact
                ).text(text.strip())
                if rendered:
                    print(rendered, file=self.out)
                else:
                    print(text.strip(), file=self.out)
                try:
                    from .gemini import resolve_model
                    route_name = resolve_model(self.model)["name"]
                except Exception:
                    route_name = str(self.model)
                sep = self.pal.dim(" · ")
                if self.compact:
                    meta = (
                        self.pal.dim("  ")
                        + self.pal.rgb(8, 103, 159, f"{elapsed:.1f}s")
                        + sep
                        + self.pal.rgb(221, 108, 54, str(model_name))
                        + sep
                        + self.pal.rgb(76, 175, 80, str(route_name))
                        + sep
                        + self.pal.dim("/copy")
                    )
                else:
                    meta = (
                        self.pal.dim(f"        {len(text.strip())} chars")
                        + sep
                        + self.pal.rgb(8, 103, 159, f"{elapsed:.1f}s")
                        + sep
                        + self.pal.dim("model ")
                        + self.pal.rgb(221, 108, 54, str(model_name))
                        + sep
                        + self.pal.rgb(76, 175, 80, str(route_name))
                        + sep
                        + self.pal.dim("/copy")
                    )
                self._print(meta)
            return AgentResult(reply=text.strip(), rounds=1, done=True, touched_files=[])

        self._print(self.pal.dim(f"  📁 {self.ws.root}"))
        if self.ws.root == Path.home():
            self._print(self.pal.yellow(
                "  ⚠ выбрана домашняя папка; лучше указать папку проекта: /workspace <путь>"
            ))

        files: dict[str, str] = {}
        rounds = 0

        # Фаза 1: исследование
        redundant_reads: list[str] = []
        for _ in range(self.max_rounds):
            rounds += 1
            prompt = build_research_prompt(
                task, self.ws.tree(), files, self.context_chars,
                history=self.history, recent_files=self.recent_files,
            )
            if redundant_reads:
                hint = (
                    "\n\n<note>\n"
                    "Some requested files/ranges are already in context: "
                    + "; ".join(redundant_reads[:6])
                    + ". Do not request them again; proceed to plan/edits or answer READY."
                    + "\n</note>"
                )
                prompt += hint
                redundant_reads = []
            text = self._ask(prompt, "scan: reading project")
            try:
                plan = parse_agent_response(text)
            except AgentError as exc:
                self._print(self.pal.red(f"  ⚠ parse error: {exc}"))
                break
            if not plan.reads:
                break
            new_reads = 0
            for spec in plan.reads[:12]:
                if self._read_into(files, spec, redundant_reads):
                    key = format_read_key(*parse_read_spec(spec))
                    if not files[key].startswith("[не удалось прочитать:"):
                        new_reads += 1
            if not new_reads:
                # all requested reads were redundant/already loaded -> stop scanning
                break

        applied: list[str] = []
        ran: list[dict] = []
        backup: Path | None = None
        last_reply = ""
        feedback = ""
        max_cycles = self.max_edit_rounds + self.max_fix_rounds + 2
        no_progress = 0

        # Фаза 2: изменения, проверки, исправления
        for _ in range(max_cycles):
            rounds += 1
            prompt = build_edit_prompt(
                task,
                self.ws.tree(),
                files,
                applied,
                feedback,
                self.context_chars,
                history=self.history,
                recent_files=self.recent_files,
            )
            text = self._ask(prompt, "plan: preparing changes/checks")
            last_reply = text
            try:
                plan = parse_agent_response(text)
            except AgentError as exc:
                feedback = f"Parse error in previous response: {exc}. Fix block headers/body and try again."
                self._print(self.pal.red(f"  ⚠ {exc}"))
                no_progress += 1
                if no_progress >= 3:
                    return AgentResult(
                        applied=bool(applied), dry_run=self.dry_run, changed=applied,
                        ran=ran, backup=backup, rounds=rounds,
                        reply=last_reply, error=str(exc),
                        touched_files=_touched(files, applied),
                    )
                continue
            feedback_parts: list[str] = []
            progressed = False

            if plan.notes:
                self._print(plan.notes[:1500])

            redundant_read_hints: list[str] = []
            if plan.reads:
                new_reads = 0
                for spec in plan.reads[:12]:
                    if self._read_into(files, spec, redundant_read_hints):
                        key = format_read_key(*parse_read_spec(spec))
                        if not files[key].startswith("[не удалось прочитать:"):
                            new_reads += 1
                if new_reads:
                    feedback_parts.append(
                        "Files read. Continue: make changes, run a check, or output DONE."
                    )
                    progressed = True
                elif redundant_read_hints:
                    feedback_parts.append(
                        "Requested files/ranges are already fully in context: "
                        + "; ".join(redundant_read_hints[:6])
                        + ". Do not request them again; proceed to plan/edits or output DONE."
                    )
                else:
                    feedback_parts.append(
                        "No readable files were loaded. Proceed to plan/edits or output DONE."
                    )

            # Применение изменений
            applied_now = False
            if plan.ops:
                try:
                    prepared = prepare_ops(self.ws, plan.ops)
                except AgentError as exc:
                    feedback = f"Ошибка в предыдущем ответе: {exc}. Исправь и повтори."
                    self._print(self.pal.red(f"  ⚠ {exc}"))
                    no_progress += 1
                    if no_progress >= 3:
                        return AgentResult(
                            applied=bool(applied), dry_run=False, changed=applied,
                            ran=ran, backup=backup, rounds=rounds,
                            reply=last_reply, error=str(exc),
                            touched_files=_touched(files, applied),
                        )
                    continue

                if prepared:
                    self.show_plan(prepared)
                    if self.dry_run:
                        self._print(self.pal.yellow("  sys  · dry-run: files not changed"))
                        if plan.runs:
                            self._print(self.pal.dim("  sys  · requested commands (not run in dry-run):"))
                            for cmd in plan.runs:
                                self._print(self.pal.dim(f"    $ {cmd}"))
                        changed_now = [item.path for item in prepared]
                        return AgentResult(
                            applied=False,
                            dry_run=True,
                            changed=changed_now,
                            ran=[],
                            rounds=rounds,
                            reply=last_reply,
                            done=plan.done,
                            touched_files=_touched(files, applied, changed_now),
                        )
                    if not self._approve():
                        self._print(self.pal.dim("  sys  · cancelled by user"))
                        return AgentResult(
                            applied=bool(applied), dry_run=False, changed=applied,
                            ran=ran, backup=backup, rounds=rounds, reply=last_reply,
                            touched_files=_touched(files, applied),
                        )
                    try:
                        backup = apply_prepared(self.ws, prepared, self.backup_root, task)
                    except AgentError as exc:
                        return AgentResult(
                            error=str(exc), changed=applied, ran=ran, backup=backup,
                            rounds=rounds, reply=last_reply,
                            touched_files=_touched(files, applied),
                        )
                    applied.extend(item.path for item in prepared)
                    for item in prepared:
                        if item.new is None:
                            files.pop(item.path, None)
                            # удаляем и диапазонные ключи этого файла
                            stale = [k for k in files if k == item.path or k.startswith(item.path + ":")]
                            for k in stale:
                                files.pop(k, None)
                        else:
                            files[item.path] = item.new
                            stale = [k for k in files if k.startswith(item.path + ":")]
                            for k in stale:
                                files.pop(k, None)
                    self._print(self.pal.green(f"  sys  · apply   ✓ {len(prepared)} file(s) changed"))
                    feedback_parts.append(
                        "Applied changes: " + ", ".join(item.path for item in prepared)
                    )
                    progressed = True
                    applied_now = True
                else:
                    self._print(self.pal.dim("  sys  · no changes"))

            # Запуск команд/проверок
            run_failed = False
            if plan.runs:
                run_records, run_feedback, run_failed = self._do_runs(plan.runs)
                ran.extend(run_records)
                feedback_parts.append(run_feedback)
                progressed = True
                if run_failed:
                    feedback_parts.append(
                        "Command failed. Fix the cause, read files/logs if needed, then verify again or output DONE if this is expected."
                    )

            if progressed:
                no_progress = 0
            else:
                no_progress += 1

            # Решение о продолжении
            if run_failed:
                feedback = "\n\n".join(feedback_parts)
                continue

            # Successful apply with no requested checks -> finish immediately
            if applied_now and not plan.runs:
                break

            if plan.done and not feedback_parts:
                break
            if plan.done and not run_failed:
                # successful checks/edits or nothing pending
                break
            if feedback_parts:
                feedback = "\n\n".join(feedback_parts)
                continue

            # Ничего полезного
            if no_progress >= 2:
                break
            feedback = (
                "Requested files/ranges are already in context or no actions were taken. "
                "Proceed with edits/run if needed, or output DONE if the task is complete."
            )

        return AgentResult(
            applied=bool(applied),
            dry_run=False,
            changed=applied,
            ran=ran,
            backup=backup,
            rounds=rounds,
            reply=last_reply,
            done=True if applied or not ran else False,
            touched_files=_touched(files, applied),
        )



