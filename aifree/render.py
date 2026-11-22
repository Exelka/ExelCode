"""Terminal markdown rendering: colors, wrapping, streaming."""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap

from .highlight import highlight_line, new_state

RESET = "\033[0m"


class Palette:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}{RESET}"

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def orange(self, text: str) -> str:
        return self._wrap("38;5;208", text)

    def rgb(self, r: int, g: int, b: int, text: str) -> str:
        return self._wrap(f"38;2;{r};{g};{b}", text)

    def blue(self, text: str) -> str:
        return self._wrap("34", text)

    def magenta(self, text: str) -> str:
        return self._wrap("35", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def white(self, text: str) -> str:
        return self._wrap("37", text)


def is_termux() -> bool:
    """Похоже ли окружение на Termux (узкий экран, мобильная клавиатура)."""
    prefix = os.environ.get("PREFIX", "")
    return (
        "com.termux" in prefix
        or bool(os.environ.get("TERMUX_VERSION"))
        or os.environ.get("EXELCODE_TERMUX") == "1"
    )


def compact_mode(width: int | None = None, force: bool | None = None) -> bool:
    """Компактный вывод для узких терминалов и Termux.

    force=True/False включает/выключает принудительно (настройка compact_ui).
    """
    if force is not None:
        return bool(force)
    env = os.environ.get("EXELCODE_COMPACT", "").strip().lower()
    if env in ("1", "on", "true", "yes", "да"):
        return True
    if env in ("0", "off", "false", "no", "нет"):
        return False
    if is_termux():
        return True
    return (width if width is not None else terminal_width()) < 56


def terminal_width(default: int = 88) -> int:
    """Current terminal width (adaptive), with safe fallbacks for Termux/pipes."""
    try:
        width = shutil.get_terminal_size((0, 0)).columns
    except OSError:
        width = 0
    if not width or width <= 0:
        try:
            width = int(os.environ.get("COLUMNS", "0") or "0")
        except (TypeError, ValueError):
            width = 0
    if not width or width <= 0:
        width = default
    # keep usable on narrow phones (Termux) and wide desktops
    return max(20, min(int(width), 300))


def strip_inline(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", text)
    return text


def _box_top(label: str, width: int) -> str:
    inner = max(10, width - 2)
    text = f"─ {label} " if label else "─"
    if len(text) > inner:
        text = text[: inner - 1] + "…"
    return "┌" + text + "─" * max(0, inner - len(text)) + "┐"


def _box_bottom(width: int) -> str:
    inner = max(10, width - 2)
    return "└" + "─" * inner + "┘"


class MarkdownRenderer:
    def __init__(
        self,
        width: int | None = None,
        color: bool = True,
        compact: bool | None = None,
    ):
        self._fixed_width = width
        self.pal = Palette(color)
        self.compact = compact_mode(width, compact)
        self.in_code = False
        self.code_lang = ""
        self.code_state = new_state()

    @property
    def width(self) -> int:
        if self._fixed_width:
            return max(20, int(self._fixed_width))
        return terminal_width()

    def _box_width(self) -> int:
        # fit narrow Termux screens; cap for readability on wide terminals
        if self.compact:
            return max(18, min(self.width, 78))
        return max(20, min(self.width - 2, 78)) if self.width < 26 else max(24, min(self.width, 78))

    # ── one line ───────────────────────────────────────────────────────
    def line(self, raw: str) -> str:
        raw = raw.rstrip()
        stripped = raw.strip()

        if stripped.startswith("```"):
            bw = self._box_width()
            if self.in_code:
                self.in_code = False
                self.code_lang = ""
                return self.pal.dim(_box_bottom(bw))
            self.in_code = True
            self.code_lang = stripped[3:].strip()
            self.code_state = new_state()
            label = self.code_lang or "code"
            return self.pal.dim(_box_top(label, bw))

        if self.in_code:
            bw = self._box_width()
            inner = max(10, bw - 4)
            text = raw.replace("\t", "    ")
            chunks = textwrap.wrap(
                text,
                width=inner,
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
            ) or [""]
            out = []
            for part in chunks:
                painted = highlight_line(
                    part,
                    self.code_lang,
                    self.code_state,
                    enabled=self.pal.enabled,
                )
                pad = " " * max(0, inner - len(part))
                out.append(self.pal.dim("│ ") + painted + pad + self.pal.dim(" │"))
            return "\n".join(out)

        if not stripped:
            return ""

        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", stripped)
        if heading:
            text = strip_inline(heading.group(2))
            color = self.pal.cyan if len(heading.group(1)) <= 2 else self.pal.magenta
            return color(self.pal.bold(text))

        if re.fullmatch(r"[-*_]{3,}", stripped):
            return self.pal.dim("─" * max(10, self.width - 2))

        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        if bullet:
            indent = bullet.group(1)
            body = strip_inline(bullet.group(2))
            avail = max(12 if self.compact else 20, self.width - len(indent) - 4)
            chunks = textwrap.wrap(
                body, width=avail, break_long_words=False, break_on_hyphens=False
            ) or [""]
            first = indent + self.pal.cyan("•") + " " + chunks[0]
            rest = [indent + "  " + part for part in chunks[1:]]
            return "\n".join([first] + rest)

        numbered = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", raw)
        if numbered:
            indent, number, body = numbered.groups()
            prefix = f"{indent}{number}. "
            avail = max(12 if self.compact else 20, self.width - len(prefix) - 2)
            chunks = textwrap.wrap(
                strip_inline(body),
                width=avail,
                break_long_words=False,
                break_on_hyphens=False,
            ) or [""]
            first = self.pal.cyan(prefix) + chunks[0]
            rest = [" " * len(prefix) + part for part in chunks[1:]]
            return "\n".join([first] + rest)

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            body = strip_inline(quote.group(1))
            chunks = textwrap.wrap(
                body, width=max(20, self.width - 4), break_long_words=False
            ) or [""]
            return "\n".join(self.pal.dim("│ " + part) for part in chunks)

        if stripped.startswith("|") and stripped.endswith("|"):
            return self.pal.dim(stripped[: self.width])

        body = strip_inline(raw)
        return "\n".join(
            textwrap.wrap(
                body,
                width=self.width,
                break_long_words=False,
                break_on_hyphens=False,
                replace_whitespace=False,
            )
            or [""]
        )

    # ── full text ──────────────────────────────────────────────────────
    def text(self, text: str) -> str:
        lines = []
        for raw in text.replace("\r\n", "\n").split("\n"):
            rendered = self.line(raw)
            if rendered:
                lines.append(rendered)
        return "\n".join(lines)


class StreamRenderer:
    """Prints response as it arrives, line by line."""

    def __init__(
        self,
        width: int | None = None,
        color: bool = True,
        out=None,
        compact: bool | None = None,
    ):
        self.md = MarkdownRenderer(width=width, color=color, compact=compact)
        self.out = out or sys.stdout
        self.buffer = ""
        self.printed = False
        self.last_blank = True
        self.any_text = False

    def feed(self, chunk: str) -> None:
        self.buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._emit(line)

    def _emit(self, line: str) -> None:
        rendered = self.md.line(line)
        if not rendered:
            if not self.last_blank:
                self.out.write("\n")
                self.last_blank = True
            return
        self.out.write(rendered + "\n")
        self.out.flush()
        self.printed = True
        self.last_blank = False
        self.any_text = True

    def finish(self) -> None:
        if self.buffer:
            self._emit(self.buffer)
            self.buffer = ""
        if self.printed and not self.last_blank:
            self.out.write("\n")
            self.out.flush()
            self.last_blank = True
        if not self.any_text:
            self.out.write(self.md.pal.dim("(empty response)") + "\n")
            self.out.flush()
