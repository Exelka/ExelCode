"""Диагностика окружения и Gemini-протокола (`/debug`, `--debug`)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import CONFIG_FILE, DATA_DIR, load_config
from . import proxy as proxy_mod
from .gemini import PAGE_URL, fetch_bl
from .render import compact_mode, is_termux, terminal_width
from .update import load_cached_update


def _run(args: list[str], cwd: Path | None = None, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:160] if text else "(empty)"


def _git_rows(workspace: Path) -> list[tuple[str, str, str]]:
    if not shutil.which("git"):
        return [("Git", "git", "не найден")]
    root = workspace
    if not (root / ".git").exists():
        top = _run(["git", "rev-parse", "--show-toplevel"], root)
        if top.startswith(("fatal:", "error:")):
            return [("Git", "репозиторий", "не найден")]
        candidate = Path(top)
        if candidate.is_dir():
            root = candidate
    return [
        ("Git", "корень", str(root)),
        ("Git", "ветка", _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)),
        ("Git", "коммит", _run(["git", "rev-parse", "--short", "HEAD"], root)),
        ("Git", "изменения", _run(["git", "status", "--porcelain"], root)),
        ("Git", "gh", shutil.which("gh") or "не найден"),
    ]


def _proxy_mode() -> str:
    try:
        st = proxy_mod.status()
        return f"{st['mode']} ({st['count']} прокси, {st['user_count']} своих)"
    except Exception:
        return "?"


def collect_diagnostics(
    workspace: str | Path | None = None,
    live: bool = True,
) -> list[tuple[str, str, str]]:
    """Собирает пары (секция, параметр, значение). Без секретов и токенов."""
    ws = Path(workspace or Path.cwd()).expanduser().resolve()
    setting = str(load_config().get("compact_ui", "auto")).strip().lower()
    force: bool | None = None
    if setting in ("on", "1", "yes", "вкл", "true"):
        force = True
    elif setting in ("off", "0", "no", "выкл", "false"):
        force = False
    rows: list[tuple[str, str, str]] = [
        ("ExelCode", "версия", __version__),
        ("ExelCode", "Python", f"{platform.python_version()} ({sys.executable})"),
        ("ExelCode", "платформа", f"{platform.system()} {platform.release()} · {platform.machine()}"),
        ("ExelCode", "терминал", f"{terminal_width()} cols · tty={sys.stdout.isatty()}"),
        ("ExelCode", "компактный UI", f"{'да' if compact_mode(force=force) else 'нет'} ({setting})"),
        ("ExelCode", "Termux", "да" if is_termux() else "нет"),
        ("ExelCode", "кодировка", f"stdin={sys.stdin.encoding} stdout={sys.stdout.encoding}"),
        ("Пути", "рабочая папка", str(ws)),
        ("Пути", "cwd", str(Path.cwd())),
        ("Пути", "данные", str(DATA_DIR)),
        ("Пути", "конфиг", f"{CONFIG_FILE} ({'есть' if CONFIG_FILE.is_file() else 'нет'})"),
        ("Пути", "EXELCODE_HOME", os.environ.get("EXELCODE_HOME", "—")),
        ("Сеть", "прокси (env)", ", ".join(
            f"{key}={os.environ[key]}"
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
            if os.environ.get(key)
        ) or "не задан"),
        ("Сеть", "прокси (ExelCode)", _proxy_mode()),
        ("Сеть", "URL", PAGE_URL),
    ]
    rows.extend(_git_rows(ws))

    cached = load_cached_update()
    if cached:
        age_h = max(0.0, time.time() - cached.checked_at) / 3600
        rows.append(("Обновления", "кэш", f"{cached.latest} · {age_h:.1f} ч назад"))
    else:
        rows.append(("Обновления", "кэш", "нет"))

    if live:
        try:
            bl = fetch_bl(force=True, timeout=10.0)
            rows.append(("Gemini", "cfb2h", "ok: " + bl[:32]))
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
            rows.append(("Gemini", "cfb2h", f"ошибка: {exc}"))
    else:
        rows.append(("Gemini", "cfb2h", "проверка отключена"))
    return rows


def format_diagnostics(rows: list[tuple[str, str, str]]) -> str:
    lines: list[str] = ["Диагностика ExelCode", ""]
    current = ""
    for section, key, value in rows:
        if section != current:
            current = section
            lines.append(f"[{section}]")
        lines.append(f"  {key:<22} {value}")
    return "\n".join(lines)
