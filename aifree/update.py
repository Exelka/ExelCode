"""Check for updates on GitHub and install them."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .config import DATA_DIR

REPO = "Exelka/ExelCode"
VERSION_FILE = "aifree/__init__.py"
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


CACHE_TTL = 6 * 3600.0
CACHE_FILE = DATA_DIR / "update_check.json"


@dataclass
class UpdateInfo:
    current: str
    latest: str | None = None
    url: str = ""
    error: str | None = None
    cached: bool = False
    checked_at: float = 0.0

    @property
    def available(self) -> bool:
        if not self.latest:
            return False
        return version_tuple(self.latest) > version_tuple(self.current)

    @property
    def ahead(self) -> bool:
        if not self.latest:
            return False
        return version_tuple(self.current) > version_tuple(self.latest)


def version_tuple(version: str) -> tuple:
    parts = []
    for part in re.split(r"[.\-+]", str(version).strip()):
        if part.isdigit():
            parts.append((0, int(part), ""))
        else:
            m = re.match(r"(\d+)(.*)", part)
            if m:
                parts.append((0, int(m.group(1)), m.group(2)))
            elif part:
                parts.append((1, 0, part))
    return tuple(parts) or ((0, 0, ""),)


def parse_version_text(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1).strip() if m else None


def _repo_root() -> Path | None:
    # ExelCode/aifree/update.py -> ExelCode
    root = Path(__file__).resolve().parents[1]
    if (root / ".git").exists():
        return root
    return None


def _get_token() -> str | None:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(key)
        if token:
            return token.strip()
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        proc = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            token = (proc.stdout or "").strip()
            if token:
                return token
    except Exception:
        return None
    return None


def _fetch_via_api(timeout: float) -> str:
    token = _get_token()
    url = f"https://api.github.com/repos/{REPO}/contents/{VERSION_FILE}?ref=main"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"ExelCode/{__version__}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    content = data.get("content")
    if not content:
        raise RuntimeError("no content in GitHub response")
    text = base64.b64decode(content).decode("utf-8", "replace")
    version = parse_version_text(text)
    if not version:
        raise RuntimeError("could not parse remote version")
    return version


def _fetch_via_gh(timeout: float) -> str:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("gh not found")
    proc = subprocess.run(
        [gh, "api", f"repos/{REPO}/contents/{VERSION_FILE}", "--jq", ".content"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "gh api failed"
        raise RuntimeError(err.splitlines()[-1][:200])
    raw = "".join((proc.stdout or "").split())
    if not raw:
        raise RuntimeError("empty response from gh")
    text = base64.b64decode(raw).decode("utf-8", "replace")
    version = parse_version_text(text)
    if not version:
        raise RuntimeError("could not parse remote version")
    return version


def check_update(timeout: float = 10.0) -> UpdateInfo:
    info = UpdateInfo(current=__version__, url=f"https://github.com/{REPO}")
    errors = []
    for fetcher in (_fetch_via_api, _fetch_via_gh):
        try:
            info.latest = fetcher(timeout)
            info.error = None
            return info
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    info.error = errors[-1] if errors else "could not check updates"
    return info


def _cache_path(cache_file: str | Path | None = None) -> Path:
    return Path(cache_file) if cache_file else CACHE_FILE


def load_cached_update(cache_file: str | Path | None = None) -> UpdateInfo | None:
    """Читает последнюю успешную проверку обновлений (если есть)."""
    path = _cache_path(cache_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    latest = str(data.get("latest") or "").strip()
    if not latest:
        return None
    checked_at = float(data.get("checked_at", 0) or 0)
    return UpdateInfo(
        current=__version__,
        latest=latest,
        url=str(data.get("url") or f"https://github.com/{REPO}"),
        cached=True,
        checked_at=checked_at,
    )


def save_cached_update(
    info: UpdateInfo, cache_file: str | Path | None = None
) -> None:
    if info.error or not info.latest:
        return
    path = _cache_path(cache_file)
    payload = {
        "latest": info.latest,
        "url": info.url,
        "checked_at": info.checked_at or time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def check_update_cached(
    timeout: float = 6.0,
    ttl: float = CACHE_TTL,
    force: bool = False,
    cache_file: str | Path | None = None,
    checker: Callable[[float], UpdateInfo] | None = None,
) -> UpdateInfo:
    """Проверка обновлений с кэшем: не дёргает сеть чаще, чем раз в ttl."""
    if not force:
        cached = load_cached_update(cache_file)
        if cached and time.time() - cached.checked_at <= max(0.0, ttl):
            return cached
    fetch = checker or check_update
    info = fetch(timeout)
    if not info.error and info.latest:
        info.checked_at = time.time()
        save_cached_update(info, cache_file)
    return info


_ASYNC_LOCK = threading.Lock()
_ASYNC: dict = {"thread": None, "info": None, "taken": True}


def start_background_update_check(
    timeout: float = 6.0,
    ttl: float = CACHE_TTL,
    cache_file: str | Path | None = None,
    force: bool = False,
    checker: Callable[[float], UpdateInfo] | None = None,
) -> bool:
    """Запускает проверку обновлений в фоне, не блокируя запуск."""
    with _ASYNC_LOCK:
        thread = _ASYNC.get("thread")
        if thread is not None and thread.is_alive():
            return False
        _ASYNC["info"] = None
        _ASYNC["taken"] = False
        thread = threading.Thread(
            target=_background_worker,
            args=(timeout, ttl, cache_file, force, checker),
            name="exelcode-update-check",
            daemon=True,
        )
        _ASYNC["thread"] = thread
        thread.start()
        return True


def _background_worker(
    timeout: float,
    ttl: float,
    cache_file,
    force: bool = False,
    checker: Callable[[float], UpdateInfo] | None = None,
) -> None:
    try:
        info = check_update_cached(
            timeout=timeout, ttl=ttl, cache_file=cache_file, force=force, checker=checker
        )
    except Exception as exc:  # noqa: BLE001 — фоновая проверка не должна падать
        info = UpdateInfo(current=__version__, error=str(exc))
    with _ASYNC_LOCK:
        _ASYNC["info"] = info


def take_background_update() -> UpdateInfo | None:
    """Возвращает результат фоновой проверки один раз (если готов)."""
    with _ASYNC_LOCK:
        info = _ASYNC.get("info")
        if info is None or _ASYNC.get("taken"):
            return None
        _ASYNC["taken"] = True
        return info


def install_update(timeout: float = 120.0) -> tuple[bool, str]:
    root = _repo_root()
    if not root:
        return False, "not a git checkout; update manually from " + f"https://github.com/{REPO}"

    git = shutil.which("git")
    if not git:
        return False, "git not found"

    def run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [git, *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    # fetch first
    proc = run(["fetch", "origin", "main"])
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "git fetch failed").strip()
        return False, msg.splitlines()[-1][:300]

    # try pull (ff-only; autostash helps with local changes like this session)
    proc = run(["pull", "--ff-only", "--autostash", "origin", "main"])
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "git pull failed").strip()
        # common case: local commits/divergence
        return False, msg.splitlines()[-1][:300]

    out = (proc.stdout or "").strip() or "updated"
    return True, out.splitlines()[-1][:300]


def restart_program(argv: list[str] | None = None) -> None:
    """Restart current process with the same Python interpreter and argv."""
    args = [sys.executable] + (sys.argv if argv is None else list(argv))
    try:
        # Flush stdio before replacing process
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.execv(sys.executable, args)
    except Exception:
        # Fallback (e.g. some environments where execv behaves oddly)
        os.execvp(sys.executable, args)


def startup_update_check(
    *,
    interactive: bool,
    prompt_yes_no: Callable[[str], bool] | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> bool:
    """Auto-check for updates on startup.

    Returns True if an update was installed and process restart was initiated.
    Silent when up to date or on check failure.
    """
    if not interactive:
        return False
    try:
        info = check_update_cached(timeout=6.0, force=True)
    except Exception:
        return False
    if info.error or not info.available:
        return False

    say = print_fn or print
    ask = prompt_yes_no
    if ask is None:
        if not sys.stdin.isatty():
            return False

        def ask(q: str) -> bool:
            try:
                ans = input(q).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return ans in ("y", "yes", "д", "да")

    say(f"Доступно обновление: {info.latest} (текущая {info.current})")
    if not ask("Установить обновление и перезапустить сейчас? [y/N]: "):
        return False

    ok, msg = install_update(timeout=180.0)
    if not ok:
        say(f"Ошибка обновления: {msg}")
        return False
    say(f"Обновлено: {msg}")
    say("Перезапускаю ExelCode...")
    restart_program()
    return True  # not reached on successful exec, but for clarity

