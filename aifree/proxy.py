"""Прокси для исходящих запросов Gemini (urllib).

Режимы:
- off: никогда не использовать прокси.
- auto: прямое соединение; при сетевой ошибке пробовать прокси по очереди.
- always: сразу использовать прокси (прямое соединение не пробовать).
"""

from __future__ import annotations

import os
import random
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DATA_DIR

BUNDLED = Path(__file__).with_name("proxies.txt")
USER_FILE = DATA_DIR / "proxies.txt"

_LOCK = threading.Lock()
_STATE = {
    "mode": "auto",  # off | auto | always
    "dead_until": {},  # proxy -> monotonic timestamp
}
_DEAD_TTL = 5 * 60.0  # не повторять мёртвый прокси 5 минут
_NOTIFY = None
_NOTIFY_LOCK = threading.Lock()


def normalize_proxy(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    # только http прокси; https CONNECT через http proxy поддерживается urllib
    if not raw.startswith(("http://", "https://")):
        return None
    # простая валидация host:port
    rest = raw.split("://", 1)[1].rstrip("/")
    if not rest or ":" not in rest.rsplit("@", 1)[-1]:
        return None
    return raw


def load_proxies() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in (BUNDLED, USER_FILE):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            p = normalize_proxy(line)
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def user_proxies() -> list[str]:
    try:
        lines = USER_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        p = normalize_proxy(line)
        if p:
            out.append(p)
    return out


def add_user_proxy(value: str) -> str:
    p = normalize_proxy(value)
    if not p:
        raise ValueError("укажи прокси как host:port или http://host:port")
    items = user_proxies()
    if p in items:
        return p
    items.append(p)
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_FILE.write_text("\n".join(items) + "\n", encoding="utf-8")
    return p


def remove_user_proxy(value: str) -> bool:
    target = (value or "").strip()
    items = user_proxies()
    kept = []
    removed = False
    # allow remove by 1-based index in user list
    if target.isdigit():
        idx = int(target) - 1
        for i, p in enumerate(items):
            if i == idx:
                removed = True
            else:
                kept.append(p)
    else:
        norm = normalize_proxy(target) or target
        for p in items:
            if p == norm or p == target:
                removed = True
            else:
                kept.append(p)
    if removed:
        USER_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_FILE.write_text(
            ("\n".join(kept) + "\n") if kept else "", encoding="utf-8"
        )
    return removed


def set_notify(fn) -> None:
    """Устанавливает колбэк для сообщений о fallback (fn(str) -> None)."""
    global _NOTIFY
    with _NOTIFY_LOCK:
        _NOTIFY = fn


def _notify(msg: str) -> None:
    with _NOTIFY_LOCK:
        fn = _NOTIFY
    if not fn:
        return
    try:
        fn(msg)
    except Exception:
        pass


def get_mode() -> str:
    with _LOCK:
        mode = str(_STATE.get("mode", "auto"))
    return mode if mode in ("off", "auto", "always") else "auto"


def set_mode(mode: str) -> str:
    mode = (mode or "off").strip().lower()
    aliases = {"on": "auto", "1": "auto", "true": "auto", "yes": "auto", "вкл": "auto"}
    mode = aliases.get(mode, mode)
    if mode not in ("off", "auto", "always"):
        raise ValueError("режим: off, auto или always")
    with _LOCK:
        _STATE["mode"] = mode
    return mode


def status() -> dict:
    proxies = load_proxies()
    return {
        "mode": get_mode(),
        "count": len(proxies),
        "user_count": len(user_proxies()),
        "bundled": BUNDLED.is_file(),
        "user_file": str(USER_FILE),
    }


def _is_network_error(exc: Exception) -> bool:
    # HTTPError = получили ответ от сервера, это не "пропал интернет".
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        msg = str(exc).lower()
        for k in (
            "timed out", "temporar", "connection", "network", "name or service",
            "getaddrinfo", "unreachable", "refused", "reset",
        ):
            if k in msg:
                return True
    return False


def _alive_proxies() -> list[str]:
    now = time.monotonic()
    proxies = load_proxies()
    with _LOCK:
        dead = _STATE["dead_until"]
        # clean expired
        for p, until in list(dead.items()):
            if until <= now:
                dead.pop(p, None)
        alive = [p for p in proxies if dead.get(p, 0) <= now]
    if not alive:
        alive = proxies[:]
    # небольшая рандомизация, чтобы не долбить один и тот же
    random.shuffle(alive)
    return alive


def _mark_dead(proxy: str) -> None:
    with _LOCK:
        _STATE["dead_until"][proxy] = time.monotonic() + _DEAD_TTL


def _open_with_proxy(req: urllib.request.Request, timeout: float, proxy: str):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    return opener.open(req, timeout=timeout)


def urlopen(req: urllib.request.Request, timeout: float):
    """Открывает запрос с учётом режима прокси.

    auto: сначала прямое соединение, при сетевой ошибке — прокси по очереди.
    always: только прокси.
    off: только прямое.
    """
    mode = get_mode()
    direct_err: Exception | None = None

    if mode != "always":
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            direct_err = exc
            if mode == "off" or not _is_network_error(exc):
                raise
            proxies_count = len(load_proxies())
            _notify(
                f"прямое соединение не сработало, подключаю прокси "
                f"({proxies_count} шт.) — если один не отвечает, пробую следующий"
            )

    proxies = _alive_proxies()
    last_err: Exception | None = direct_err
    for proxy in proxies:
        try:
            resp = _open_with_proxy(req, timeout, proxy)
            # attach info for diagnostics
            try:
                resp._exelcode_proxy = proxy  # type: ignore[attr-defined]
            except Exception:
                pass
            return resp
        except urllib.error.HTTPError:
            # Дошли до сервера и получили HTTP-ответ — это не проблема прокси.
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _mark_dead(proxy)
            continue

    if last_err is not None:
        raise last_err
    raise urllib.error.URLError("no proxies available")
