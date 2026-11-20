"""Check for updates on GitHub and install them."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

REPO = "Exelka/ExelCode"
VERSION_FILE = "aifree/__init__.py"
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


@dataclass
class UpdateInfo:
    current: str
    latest: str | None = None
    url: str = ""
    error: str | None = None

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
        info = check_update(timeout=6.0)
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

