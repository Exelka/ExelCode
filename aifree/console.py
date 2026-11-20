"""Кроссплатформенные мелочи: консоль, кодировки и команды.

Модуль сознательно не тянет сторонних зависимостей: всё, что нужно,
делается средствами стандартной библиотеки и WinAPI через ctypes.
"""

from __future__ import annotations

import locale
import os
import re
import sys

IS_WINDOWS = os.name == "nt"

# python3 / python3.12 и т.п. — на Windows такого имени обычно нет.
_PY3_RE = re.compile(r"^(\s*)(python3(?:\.\d+)?)(?=\s|$)")


def setup_console() -> None:
    """UTF-8 для stdin/stdout/stderr и ANSI-коды в консоли Windows.

    Без этого на Windows с кодовой страницей cp1251/cp866 любой русский
    текст в ``print`` падает с UnicodeEncodeError, а ``/clear`` и стриминг
    печатают escape-последовательности как обычный текст.
    """
    for stream in (sys.stdout, sys.stderr):
        _reconfigure(stream, "utf-8", "replace")
    _reconfigure(sys.stdin, "utf-8", "replace")
    if IS_WINDOWS:
        _enable_ansi()


def _reconfigure(stream, encoding: str, errors: str) -> None:
    try:
        stream.reconfigure(encoding=encoding, errors=errors)
    except (AttributeError, OSError, ValueError):
        # Поток уже закрыт, подменён (например, в тестах) или не текстовый.
        pass


def _enable_ansi() -> None:
    """Включает ENABLE_VIRTUAL_TERMINAL_PROCESSING для stdout/stderr."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle_type = ctypes.c_void_p
        kernel32.GetStdHandle.restype = handle_type
        kernel32.GetStdHandle.argtypes = [ctypes.c_int]
        kernel32.GetConsoleMode.argtypes = [handle_type, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.SetConsoleMode.argtypes = [handle_type, ctypes.c_uint32]
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            if not handle or handle == handle_type(-1).value:
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 — без ANSI приложение всё равно работает
        pass


def normalize_newlines(text: str) -> str:
    """Приводит CRLF/CR к LF — так текст видят модель, diff и патчи."""
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def adapt_command(cmd: str) -> str:
    """На Windows подменяет ``python3`` на текущий интерпретатор.

    README и промпт агента написаны под Linux/Termux, где есть ``python3``.
    В Windows такого имени нет, поэтому ``python3 -m pytest`` выполнялся бы
    с кодом 9009 («команда не найдена»).
    """
    if not IS_WINDOWS:
        return cmd
    match = _PY3_RE.match(cmd)
    if not match:
        return cmd
    # Не полагаемся на shutil.which: в Windows «python3» часто оказывается
    # пустой заглушкой из Microsoft Store, которая падает с кодом 9009.
    exe = sys.executable or "python"
    if " " in exe:
        exe = f'"{exe}"'
    return f"{match.group(1)}{exe}{cmd[match.end():]}"


def utf8_child_env() -> dict:
    """Окружение для запускаемых команд.

    Python-потомки (pytest, скрипты проекта) при выводе в пайп используют
    локальную кодировку и падают на «✓»/кириллице вне cp1251. UTF-8-режим
    делает их вывод предсказуемым.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def decode_output(data: bytes) -> str:
    """Декодирует вывод команды: UTF-8, затем OEM (Windows) или локаль."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WINDOWS:
        candidates = ["oem", locale.getpreferredencoding(False)]
    else:
        candidates = ["latin-1"]
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")

