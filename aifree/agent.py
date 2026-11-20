"""Агент: исследует проект, меняет файлы и запускает проверки.

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
делается резервная копия. Команды запускаются только с подтверждения.
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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR, atomic_write
from .gemini import Gemini
from .render import Palette

MAX_WRITE_BYTES = 1_000_000
MAX_READ_BYTES = 2_000_000
MAX_RUN_OUTPUT = 8000
DEFAULT_RUN_TIMEOUT = 60.0
AGENT_MODES = ("ask", "auto", "dry-run")

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
            raise AgentError("пустой путь")
        if "\x00" in raw:
            raise AgentError("некорректный путь")
        path = Path(raw)
        if not path.parts:
            raise AgentError("пустой путь")
        if path.is_absolute():
            raise AgentError(f"абсолютный путь запрещён: {raw}")
        if any(part in ("..", "~") for part in path.parts):
            raise AgentError(f"выход за пределы проекта запрещён: {raw}")
        if any(part in IGNORED_DIRS for part in path.parts):
            raise AgentError(f"служебная папка запрещена: {raw}")
        if _is_secret(path.name):
            raise AgentError(f"секретный файл запрещён: {raw}")
        target = (self.root / path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise AgentError(f"путь вне проекта: {raw}") from exc
        raw_path = self.root / path
        if raw_path.is_symlink():
            raise AgentError(f"символические ссылки запрещены: {raw}")
        if must_exist and not target.is_file():
            raise AgentError(f"файл не найден: {raw}")
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
    value = value.strip().strip("\"'`").strip()
    quoted = re.match(r'^(["\'])(.*?)\1', value)
    if quoted:
        value = quoted.group(2)
    elif value:
        value = value.split()[0]
    if value.startswith("./"):
        value = value[2:]
    return value.strip()


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
    rest = re.sub(r"(?i)^path\s*[:=]\s*", "", rest)
    if kind in ("run", "exec", "command", "cmd", "bash", "sh", "shell"):
        # для run путь не нужен; в rest может быть команда, но берём тело блока
        quoted = re.match(r'^(["\'])(.*?)\1', rest)
        path = quoted.group(2) if quoted else rest
        return "run", path.strip().strip("\"'`")
    if not rest or rest.lower() in ("path", "путь"):
        return None
    quoted = re.match(r'^(["\'])(.*?)\1', rest)
    path = quoted.group(2) if quoted else rest.split()[0]
    path = path.strip().strip("\"'`")
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
                atomic_write(target, item.new)
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
    cwd = Path(cwd)
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        output = proc.stdout or ""
        if proc.stderr:
            if output and not output.endswith("\n"):
                output += "\n"
            output += proc.stderr
        code = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if output and not output.endswith("\n"):
            output += "\n"
        output += f"[таймаут {timeout:g} с]"
        code = 124
    except Exception as exc:  # noqa: BLE001
        output = f"[ошибка запуска: {exc}]"
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
        return "(файлы ещё не прочитаны)"
    parts: list[str] = []
    used = 0
    for rel, content in files.items():
        block = f"--- {rel} ---\n{content}\n"
        if used + len(block) > limit:
            parts.append(f"--- {rel} ---\n[не показан: лимит контекста]\n")
            continue
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def build_research_prompt(
    task: str,
    tree: str,
    files: dict[str, str],
    context_chars: int = 30000,
) -> str:
    budget = max(4000, context_chars - len(tree) - 3000)
    return f"""Ты — автономный coding-агент ai-free.
Ты работаешь в локальной папке проекта. Задача пользователя:
<task>
{task}
</task>

Дерево файлов (относительные пути):
<tree>
{tree}
</tree>

Уже прочитанные файлы:
<files>
{_format_files(files, budget)}
</files>

Сейчас ФАЗА 1 — исследование. Файлы менять нельзя.
Если для задачи нужно посмотреть файлы, выведи до 8 строк строго такого вида:
READ: относительный/путь.py
Для большого файла можно запросить фрагмент:
READ: относительный/путь.py:1-120
Если контекста достаточно, выведи одну строку:
READY
Не выводи других инструкций и не предлагай изменения на этой фазе.
"""


def build_edit_prompt(
    task: str,
    tree: str,
    files: dict[str, str],
    applied: list[str],
    feedback: str = "",
    context_chars: int = 30000,
) -> str:
    budget = max(4000, context_chars - len(tree) - 5000)
    applied_text = "\n".join(f"- {path}" for path in applied) or "(пока нет)"
    feedback_text = f"\n<feedback>\n{feedback}\n</feedback>\n" if feedback else ""
    return f"""Ты — автономный coding-агент ai-free.
Ты работаешь в локальной папке проекта. Задача пользователя:
<task>
{task}
</task>

Дерево файлов (относительные пути):
<tree>
{tree}
</tree>

Прочитанные файлы:
<files>
{_format_files(files, budget)}
</files>

Уже применённые файлы:
{applied_text}
{feedback_text}
Сейчас ФАЗА 2 — изменения. Выведи файловые блоки строго в формате Markdown.

Создать или полностью перезаписать файл:
```file path="relative/path.py"
<полное содержимое файла>
```

Изменить часть файла:
```patch path="relative/path.py"
<<<<<<< SEARCH
точный старый фрагмент
=======
новый фрагмент
>>>>>>> REPLACE
```

Удалить файл:
```delete path="relative/path.py"
```

Запросить чтение файла или фрагмента (если не хватает контекста):
READ: relative/path.py
READ: relative/path.py:1-120

Запустить команду для проверки (тесты, линтер, запуск):
```run
python3 -m pytest -q
```

Правила:
- только относительные пути внутри проекта; запрещены absolute, .., ~, .git, .env, ключи и бинарные файлы;
- SEARCH должен точно совпадать с текущим файлом и встречаться ровно один раз;
- сохраняй стиль, отступы и язык проекта;
- можно вывести несколько блоков;
- не повторяй уже применённые изменения без причины;
- если после применения нужны проверки, выведи блок run; после запуска ты получишь вывод и код возврата;
- если команда упала, исправь причину и проверь снова;
- если делать больше нечего, выведи DONE;
- не оборачивай весь ответ в дополнительный блок кода.
"""


def default_workspace(cfg: dict) -> Path:
    raw = str(cfg.get("agent_workspace") or "").strip()
    path = Path(raw).expanduser() if raw else Path.cwd()
    return path.resolve()


class Agent:
    """Агент: исследование, правки, запуск проверок, исправление по результатам."""

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
        self.client = client or Gemini(model, timeout=timeout)

    # ── ввод-вывод ──────────────────────────────────────────────────────
    def _print(self, text: str = "") -> None:
        if not self.quiet:
            print(text, file=self.out)

    def _ask(self, prompt: str, label: str) -> str:
        self._print(self.pal.dim(f"  · {label}…"))
        self.out.flush()
        reply = self.client.ask(prompt)
        text = reply.text if hasattr(reply, "text") else str(reply)
        model_name = getattr(reply, "model", "?")
        elapsed = getattr(reply, "elapsed", 0.0)
        self._print(self.pal.dim(f"    {model_name} · {elapsed:.1f} с"))
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
        print(self.pal.bold("\n  План изменений:"), file=self.out)
        for item in prepared:
            icon = colors[item.kind](icons[item.kind])
            print(f"  {icon} {item.kind:<6} {item.path}", file=self.out)
        print(file=self.out)
        for item in prepared:
            lines = diff_for(item)
            if not lines:
                continue
            print(self.pal.bold(f"  {item.path}"), file=self.out)
            shown = 0
            for line in lines:
                if shown >= self.max_diff_lines:
                    print(self.pal.dim("  … diff обрезан"), file=self.out)
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

    def _approve_run(self, cmd: str) -> bool:
        if self.auto_approve:
            return True
        short = cmd if len(cmd) <= 140 else cmd[:137] + "…"
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
    def _read_into(self, files: dict[str, str], spec: str) -> bool:
        path, start, end = parse_read_spec(spec)
        key = format_read_key(path, start, end)
        if key in files:
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
            if self.dry_run:
                self._print(self.pal.yellow("    dry-run: команда не запущена"))
                feedback_parts.append(
                    f"<run command={cmd!r} status='skipped' elapsed='0.0s'>\n"
                    "[dry-run: команда не запускалась]\n</run>"
                )
                records.append(
                    {"cmd": cmd, "code": None, "output": "", "elapsed": 0.0, "skipped": True}
                )
                continue
            if not self._approve_run(cmd):
                self._print(self.pal.dim("    пропущено"))
                feedback_parts.append(
                    f"<run command={cmd!r} status='skipped' elapsed='0.0s'>\n"
                    "[пользователь отменил запуск]\n</run>"
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
            self._print(f"  {mark} {status} · {elapsed:.1f} с")
            feedback_parts.append(format_run_feedback(cmd, code, output, elapsed))
            if code != 0:
                failed = True
        return records, "\n\n".join(feedback_parts), failed

    # ── основной цикл ──────────────────────────────────────────────────
    def run(self, task: str) -> AgentResult:
        task = task.strip()
        if not task:
            raise AgentError("пустая задача")

        # Обычный вопрос/разговор без привязки к файлам проекта — без сканирования
        if should_answer_without_scan(task):
            self._print(self.pal.dim("  · отвечаю…"))
            self.out.flush()
            prompt = (
                "Ты полезный ассистент. Ответь на русском, кратко и по делу. "
                "Если вопрос о твоих возможностях — скажи, что ты ai-free: можешь "
                "общаться, а в режиме агента создавать/изменять файлы и запускать "
                "команды в выбранной папке проекта.\n\n"
                f"Вопрос: {task}"
            )
            try:
                reply = self.client.ask(prompt)
                text = reply.text if hasattr(reply, "text") else str(reply)
                model_name = getattr(reply, "model", "?")
                elapsed = getattr(reply, "elapsed", 0.0)
            except Exception as exc:  # noqa: BLE001
                raise AgentError(f"не удалось получить ответ: {exc}") from exc
            self._print(self.pal.dim(f"    {model_name} · {elapsed:.1f} с"))
            self._print()
            if not self.quiet:
                print(text.strip(), file=self.out)
            return AgentResult(reply=text.strip(), rounds=1, done=True)

        self._print(self.pal.dim(f"  📁 {self.ws.root}"))
        if self.ws.root == Path.home():
            self._print(self.pal.yellow(
                "  ⚠ выбрана домашняя папка; лучше указать папку проекта: /workspace <путь>"
            ))

        files: dict[str, str] = {}
        rounds = 0

        # Фаза 1: исследование
        for _ in range(self.max_rounds):
            rounds += 1
            prompt = build_research_prompt(
                task, self.ws.tree(), files, self.context_chars
            )
            text = self._ask(prompt, "исследую проект")
            plan = parse_agent_response(text)
            if not plan.reads:
                break
            new_reads = 0
            for spec in plan.reads[:12]:
                if self._read_into(files, spec):
                    key = format_read_key(*parse_read_spec(spec))
                    if not files[key].startswith("[не удалось прочитать:"):
                        new_reads += 1
            if not new_reads:
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
            )
            text = self._ask(prompt, "готовлю изменения/проверки")
            last_reply = text
            plan = parse_agent_response(text)
            feedback_parts: list[str] = []
            progressed = False

            if plan.notes:
                self._print(plan.notes[:1500])

            if plan.reads:
                for spec in plan.reads[:12]:
                    self._read_into(files, spec)
                feedback_parts.append("Файлы прочитаны. Продолжай: внеси изменения, запусти проверку или выведи DONE.")
                progressed = True

            # Применение изменений
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
                        )
                    continue

                if prepared:
                    self.show_plan(prepared)
                    if self.dry_run:
                        self._print(self.pal.yellow("  dry-run: файлы не изменены"))
                        if plan.runs:
                            self._print(self.pal.dim("  запрошенные команды (не выполнены в dry-run):"))
                            for cmd in plan.runs:
                                self._print(self.pal.dim(f"    $ {cmd}"))
                        return AgentResult(
                            applied=False,
                            dry_run=True,
                            changed=[item.path for item in prepared],
                            ran=[],
                            rounds=rounds,
                            reply=last_reply,
                            done=plan.done,
                        )
                    if not self._approve():
                        self._print(self.pal.dim("  отменено пользователем"))
                        return AgentResult(
                            applied=bool(applied), dry_run=False, changed=applied,
                            ran=ran, backup=backup, rounds=rounds, reply=last_reply,
                        )
                    try:
                        backup = apply_prepared(self.ws, prepared, self.backup_root, task)
                    except AgentError as exc:
                        return AgentResult(
                            error=str(exc), changed=applied, ran=ran, backup=backup,
                            rounds=rounds, reply=last_reply,
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
                    self._print(self.pal.green(f"  ✓ применено: {len(prepared)} файл(ов)"))
                    feedback_parts.append(
                        "Изменения применены: " + ", ".join(item.path for item in prepared)
                    )
                    progressed = True
                else:
                    self._print(self.pal.dim("  изменений нет"))

            # Запуск команд/проверок
            run_failed = False
            if plan.runs:
                run_records, run_feedback, run_failed = self._do_runs(plan.runs)
                ran.extend(run_records)
                feedback_parts.append(run_feedback)
                progressed = True
                if run_failed:
                    feedback_parts.append(
                        "Команда завершилась с ошибкой. Исправь причину, при необходимости прочитай файлы/логи и повтори проверку или выведи DONE, если это ожидаемо."
                    )

            if progressed:
                no_progress = 0
            else:
                no_progress += 1

            # Решение о продолжении
            if run_failed:
                feedback = "\n\n".join(feedback_parts)
                continue
            if plan.done and not feedback_parts:
                break
            if plan.done and feedback_parts and not run_failed and not plan.ops:
                # были только успешные run'ы и DONE
                feedback = "\n\n".join(feedback_parts)
                # дадим шанс завершиться без лишнего запроса
                if any(r.get("code") == 0 and not r.get("skipped") for r in ran[-len(plan.runs):] if plan.runs):
                    break
                break
            if feedback_parts:
                if plan.done and not run_failed:
                    break
                feedback = "\n\n".join(feedback_parts)
                continue

            # Ничего полезного
            if not feedback:
                feedback = (
                    "Ты не вывел ни изменений, ни run, ни READ. "
                    "Повтори строго в формате file/patch/delete/run/READ или выведи DONE."
                )
                continue
            if no_progress >= 2:
                break
            feedback = "Нет действий. Если задача решена — выведи DONE, иначе внеси изменения или запусти проверку."

        return AgentResult(
            applied=bool(applied),
            dry_run=False,
            changed=applied,
            ran=ran,
            backup=backup,
            rounds=rounds,
            reply=last_reply,
            done=True if applied or not ran else False,
        )



