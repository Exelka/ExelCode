"""Interactive REPL and application commands."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from . import proxy as proxy_mod
from .config import (
    BACKUPS_DIR,
    CHATS_DIR,
    CONFIG_FILE,
    DATA_DIR,
    DEFAULTS,
    EXPORTS_DIR,
    HISTORY_FILE,
    MEMORY_FILE,
    save_config,
)
from .agent import (
    Agent,
    AgentError,
    assess_command,
    default_workspace,
    execute_command,
    is_action_request,
    list_backups,
    normalize_agent_mode,
    undo_last,
)
from .gemini import MODELS, Gemini, GeminiError, resolve_model
from .prompt import build_prompt, make_summary_prompt, summary_candidates
from .render import (
    MarkdownRenderer,
    Palette,
    StreamRenderer,
    compact_mode,
    is_termux,
    terminal_width,
)
from .store import Chat, ChatStore, Memory, human_time

# Справка: группы команд и подробное описание каждой команды.
# (использование, короткое описание, подробное объяснение с примерами)
HELP_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Чат",
        [
            (
                "/new [название]",
                "новый чат",
                "Создаёт новый чат и переключается на него. Текущий чат сохраняется автоматически.\n"
                "Пример: /new Книга про зомби",
            ),
            (
                "/chats",
                "сохранённые чаты",
                "Показывает чаты по убыванию активности: номер, название, число сообщений и время.\n"
                "Номер и id из списка подходят для /open и /delete.",
            ),
            (
                "/open <номер|id>",
                "открыть чат",
                "Открывает чат по номеру из /chats или по id (можно указать начало id).\n"
                "Пример: /open 2",
            ),
            (
                "/rename <название>",
                "переименовать чат",
                "Меняет название текущего чата. Без аргумента показывает текущее название.\n"
                "Пример: /rename Планы на неделю",
            ),
            (
                "/delete [номер|id|all]",
                "удалить чат/все",
                "Удаляет чат после подтверждения. Без аргумента — текущий чат.\n"
                "/delete all — удаляет все чаты и глобальную память (/remember),\n"
                "после чего создаёт новый пустой чат.",
            ),
            (
                "/reset [all]",
                "очистить историю/всё",
                "Без аргумента — очищает историю текущего чата, память о вас остаётся.\n"
                "/reset all — сбрасывает всё: настройки, чаты, память, историю ввода,\n"
                "бэкапы, экспорты, кэш обновлений и пользовательские прокси.",
            ),
            (
                "/export [файл]",
                "экспорт в markdown",
                "Сохраняет текущий чат в .md: заголовок, модель, пересказ и все сообщения.\n"
                "Без аргумента — в папку exports. Пример: /export ~/chat.md",
            ),
            (
                "/retry",
                "повторить ответ",
                "Удаляет последний ответ ассистента и заново отправляет последний вопрос.\n"
                "Полезно, когда ответ оборвался или оказался пустым.",
            ),
        ],
    ),
    (
        "Ответ и модель",
        [
            (
                "/model [имя|номер]",
                "выбрать модель",
                "Без аргумента — меню моделей. Можно указать id (auto, flash, lite),\n"
                "псевдоним (3.5, 3.6) или номер из меню. Выбор сохраняется в конфиг.",
            ),
            (
                "/models",
                "список маршрутов",
                "Показывает доступные маршруты моделей и их описание.\n"
                "Анонимно Google обычно отвечает 3.5 Flash-Lite — фактическую модель видно в подписи ответа.",
            ),
            (
                "/stream on|off",
                "стриминг ответа",
                "Включает или выключает печать ответа по мере генерации.\n"
                "Без аргумента показывает текущее состояние. Настройка сохраняется.",
            ),
            (
                "/copy",
                "копировать ответ",
                "Копирует последний ответ в буфер обмена: clip в Windows,\n"
                "wl-copy / xclip / xsel / pbcopy в Unix.",
            ),
            (
                "/system [текст]",
                "роль ассистента",
                "Задаёт системную роль и инструкции для текущего чата.\n"
                "Без аргумента показывает роль, /system reset — сбрасывает.\n"
                "Пример: /system отвечай как строгий редактор",
            ),
        ],
    ),
    (
        "Память",
        [
            (
                "/memory [clear]",
                "показать факты",
                "Показывает долговременную память — факты о вас, которые добавляются в каждый запрос.\n"
                "/memory clear — очистить всю память (с подтверждением).",
            ),
            (
                "/remember <факт>",
                "запомнить факт",
                "Добавляет факт в долговременную память. Без аргумента спросит текст.\n"
                "Пример: /remember меня зовут Алексей",
            ),
            (
                "/forget <номер>",
                "забыть факт",
                "Удаляет факт по номеру из /memory. Без аргумента показывает список.\n"
                "Пример: /forget 2",
            ),
        ],
    ),
    (
        "Агент",
        [
            (
                "/agent [задача]",
                "агент: меню или задача",
                "Без аргумента открывает меню агента. С текстом — выполняет задачу в папке проекта.\n"
                "Подкоманды: /agent workspace <путь>, /agent mode <режим>, /agent undo,\n"
                "/agent backups, /agent run <команда>. Можно начать задачу со слова\n"
                "plan, auto или ask, чтобы выбрать режим для этого запуска.\n"
                "Пример: /agent добавь тесты для парсера",
            ),
            (
                "/workspace [путь]",
                "папка проекта",
                "Задаёт папку, в которой агент читает и меняет файлы. Без аргумента спросит путь.\n"
                "\".\" — текущая папка, reset или ~ — домашняя. Путь сохраняется в конфиг.",
            ),
            (
                "/agentmode [режим]",
                "режим агента",
                "ask — спрашивать перед записью и запуском команд;\n"
                "auto — применять изменения и проверки без вопросов;\n"
                "plan (dry-run) — только показать план и diff, ничего не менять.\n"
                "Без аргумента — интерактивное меню.",
            ),
            (
                "/smart on|off",
                "умный режим",
                "Сам решает: обычный вопрос — ответ в чате, просьба создать/изменить/запустить — агент.\n"
                "При включении ставит режим auto и выключает «чат → агент».",
            ),
            (
                "/agentchat on|off",
                "все сообщения агенту",
                "Отправляет агенту каждое обычное сообщение (без /agent) в папке проекта.\n"
                "При включении выключает умный режим. Для обычного чата: /agentchat off.",
            ),
            (
                "/run <команда>",
                "выполнить команду",
                "Запускает команду в папке агента после подтверждения и показывает вывод и код возврата.\n"
                "Опасные команды (rm -rf, sudo, curl | sh и т.п.) помечаются предупреждением.",
            ),
            (
                "/undo",
                "откатить изменения",
                "Восстанавливает файлы из последнего бэкапа агента для текущей папки проекта.",
            ),
            (
                "/backups",
                "точки отката",
                "Показывает последние бэкапы агента: время, число файлов, статус и путь.",
            ),
        ],
    ),
    (
        "Обновления и диагностика",
        [
            (
                "/update",
                "обновить ExelCode",
                "Проверяет версию на GitHub. Если там новее — предлагает установить\n"
                "и перезапускает приложение после успешного обновления.\n"
                "Алиасы: /upgrade, /selfupdate",
            ),
            (
                "/check",
                "проверить Gemini",
                "Отправляет короткий тестовый запрос в Gemini и показывает время ответа и модель.\n"
                "Помогает быстро понять, работает ли связь.",
            ),
            (
                "/debug [--no-net]",
                "диагностика",
                "Показывает версию, Python, платформу, терминал, пути, прокси, git\n"
                "и доступность cfb2h. --no-net — без обращения к сети.\n"
                "Удобно приложить к багрепорту.",
            ),
            (
                "/proxy [режим|команда]",
                "прокси для Gemini",
                "off — не использовать прокси; auto (или on) — при сетевой ошибке пробовать прокси;\n"
                "always — всегда через прокси. /proxy list — список, /proxy add <host:port> — добавить свой,\n"
                "/proxy remove <n|url> — удалить из пользовательского списка.\n"
                "Пример: /proxy auto",
            ),
            (
                "/settings [ключ значение]",
                "настройки",
                "Без аргумента — все настройки и текущие значения. С ключом — показать или изменить.\n"
                "Примеры: /settings timeout 90\n"
                "          /settings compact_ui on\n"
                "          /settings agent_max_diff_lines 80",
            ),
        ],
    ),
    (
        "Интерфейс",
        [
            (
                "/stats",
                "статистика",
                "Показывает текущий чат, модель, число сообщений, пересказ, память,\n"
                "папку и режим агента, пути данных и конфига.",
            ),
            (
                "/clear",
                "очистить экран",
                "Очищает экран и заново рисует баннер в терминале.\n"
                "Когда вывод идёт в файл или пайп, просто добавляет пустую строку.",
            ),
            (
                "/compact [auto|on|off]",
                "компактный UI",
                "Управляет компактным режимом: auto — автоматически в Termux/узких терминалах,\n"
                "on — всегда компактно, off — обычный вид. По умолчанию auto.\n"
                "В Termux при активном компактном режиме под баннером показывается ✓ Termux UI.",
            ),
            (
                "/about",
                "о программе",
                "Версия ExelCode, используемый протокол и папка данных.",
            ),
            (
                "/help [команда]",
                "справка",
                "Показывает список команд по группам.\n"
                "/help <команда> — подробное описание и примеры. Пример: /help agent",
            ),
            (
                "/quit",
                "выход",
                "Сохраняет чат и историю ввода и закрывает приложение.\n"
                "Также работают /exit и /q.",
            ),
        ],
    ),
]

HELP = [(usage, short) for _, items in HELP_GROUPS for usage, short, _ in items]

HELP_DETAILS: dict[str, tuple[str, str, str]] = {
    usage.split()[0].lower(): (usage, short, detail)
    for _, items in HELP_GROUPS
    for usage, short, detail in items
}

HELP_ALIASES = {
    "/h": "/help",
    "/?": "/help",
    "/ls": "/chats",
    "/list": "/chats",
    "/rm": "/delete",
    "/title": "/rename",
    "/mem": "/remember",
    "/cls": "/clear",
    "/ws": "/workspace",
    "/amode": "/agentmode",
    "/mode": "/agentmode",
    "/do": "/agent",
    "/achat": "/agentchat",
    "/dbg": "/debug",
    "/config": "/settings",
    "/cfg": "/settings",
    "/upgrade": "/update",
    "/selfupdate": "/update",
    "/exit": "/quit",
    "/q": "/quit",
    "/version": "/about",
}

COMMANDS = [name.split()[0] for name, _ in HELP] + list(HELP_ALIASES)

_ANSI_RE = re.compile(r"(\x1b\[[0-?]*[ -/]*[@-~])")


def _rl_wrap_prompt(text: str) -> str:
    """Wrap ANSI escapes as readline non-printing markers to keep width correct."""
    if "\x1b" not in text:
        return text
    return _ANSI_RE.sub("\x01\\1\x02", text)


AGENT_MODE_NAMES = {
    "ask": "спрашивать",
    "auto": "авто",
    "dry-run": "план (без записи)",
}


class App:
    def __init__(
        self,
        store: ChatStore,
        memory: Memory,
        cfg: dict,
        chat: Chat,
        stream: bool = True,
        color: bool = True,
        ephemeral: bool = False,
        agent_auto_approve: bool = False,
        agent_dry_run: bool = False,
        agent_mode: str | None = None,
    ):
        self.store = store
        self.memory = memory
        self.cfg = cfg
        self.chat = chat
        self.stream = stream
        self.color = color
        self.ephemeral = ephemeral
        mode = agent_mode or cfg.get("agent_mode", "ask")
        if bool(cfg.get("agent_auto_approve")) and not agent_mode:
            mode = "auto"
        self.agent_mode = normalize_agent_mode(mode)
        if agent_dry_run:
            self.agent_mode = "dry-run"
        if agent_auto_approve:
            self.agent_mode = "auto"
        self.agent_chat = bool(cfg.get("agent_chat", False))
        self.smart_route = bool(cfg.get("smart_route", False))
        try:
            self.workspace = default_workspace(cfg)
        except (OSError, RuntimeError):
            self.workspace = Path.cwd()
        self.pal = Palette(color)
        proxy_mod.set_notify(self._proxy_notify)
        self._width = None
        self.out = sys.stdout
        self.tty = sys.stdout.isatty()
        self.last_prompt = ""
        self.last_answer = ""
        self.last_error = ""
        self.recent_agent_files: list[str] = list(
            getattr(chat, "recent_files", []) or []
        )[-20:]
        self.compact = compact_mode(force=self._compact_force())
        self.client: Gemini | None = None
        self._readline = self._setup_readline()

    def _compact_force(self) -> bool | None:
        value = str(self.cfg.get("compact_ui", "auto")).strip().lower()
        if value in ("on", "1", "yes", "вкл", "true"):
            return True
        if value in ("off", "0", "no", "выкл", "false"):
            return False
        return None

    def _refresh_compact(self) -> None:
        self.compact = compact_mode(force=self._compact_force())

    @property
    def width(self) -> int:
        try:
            return terminal_width()
        except Exception:
            return 88

    # ── запуск ─────────────────────────────────────────────────────────
    def run(self) -> int:
        if self.tty:
            self.out.write("\033[2J\033[3J\033[H")
            self.out.flush()
        self.banner()
        status_lines = []
        if self.smart_route:
            status_lines.append("  ✓ умный режим: вкл")
        if self.agent_chat:
            status_lines.append("  ✓ чат→агент: вкл")
        proxy_mode = proxy_mod.get_mode()
        if proxy_mode != "off":
            status_lines.append(f"  ✓ прокси: {proxy_mode}")
        if is_termux() and self.compact:
            status_lines.append("  ✓ Termux UI")
        for line in status_lines:
            print(self.pal.green(line))
        if status_lines:
            print()
        if self.tty:
            self._startup_update_check()
        try:
            while True:
                try:
                    line = self._read_input()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue
                if line.startswith("/"):
                    if self.command(line) == "quit":
                        break
                elif self.agent_chat:
                    self._run_agent_task(line)
                elif self.smart_route and is_action_request(line):
                    print(self.pal.dim("sys  · маршрут  обнаружено действие → агент (авто)"))
                    self._run_agent_task(line)
                else:
                    self.send(line)
        finally:
            self._save()
            self._write_history()
            print(self.pal.dim("sys  пока"))
        return 0

    def _startup_update_check(self) -> None:
        from .update import check_update_cached, install_update, restart_program

        try:
            info = check_update_cached(timeout=6.0)
        except Exception:
            return
        if info.error or not info.available:
            return
        print()
        print(self.pal.yellow(
            f"sys   доступно обновление: {info.latest} (текущая {info.current})"
        ))
        if not self._confirm("sys   установить и перезапустить?"):
            print(self.pal.dim("sys   пропущено"))
            return
        print(self.pal.dim("sys   устанавливаю обновление…"))
        ok, msg = install_update(timeout=180.0)
        if ok:
            print(self.pal.green(f"sys   ✓ {msg}"))
            print(self.pal.dim("sys   перезапускаю ExelCode..."))
            restart_program()
        else:
            print(self.pal.red(f"sys   ✗ {msg}"))


    def one_shot(self, text: str, as_json: bool = False) -> int:
        if not text.strip():
            print("пустой запрос", file=sys.stderr)
            return 2
        prompt = build_prompt(
            self.chat, self.memory.items, text, int(self.cfg["max_context_chars"])
        )
        self.last_prompt = prompt
        self.chat.add("user", text)
        self.chat.auto_title()
        if not self.ephemeral:
            self.store.save(self.chat)

        client = Gemini(self.chat.model, timeout=float(self.cfg["timeout"]))
        try:
            reply = client.ask(prompt)
        except GeminiError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        self.chat.add("assistant", reply.text, model=reply.model)
        self.last_answer = reply.text
        if not self.ephemeral:
            self.store.save(self.chat)
        if as_json:
            import json

            print(
                json.dumps(
                    {
                        "text": reply.text,
                        "model": reply.model,
                        "elapsed": round(reply.elapsed, 2),
                        "chat": self.chat.id,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(reply.text)
        return 0

    # ── ввод ───────────────────────────────────────────────────────────
    def _input(self, prompt: str) -> str:
        if self._readline:
            prompt = _rl_wrap_prompt(prompt)
        return input(prompt)

    def _read_input(self) -> str | None:
        print()
        label = "agent › " if self.agent_chat else "user  › "
        line = self._input(self.pal.cyan(label))
        if not line.endswith("\\"):
            return line
        chunks = [line[:-1]]
        while True:
            more = self._input(self.pal.dim("... "))
            if more.endswith("\\"):
                chunks.append(more[:-1])
            else:
                chunks.append(more)
                break
        return "\n".join(chunks)

    def _setup_readline(self):
        try:
            import readline
        except ImportError:
            return None
        readline.set_history_length(2000)
        try:
            readline.read_history_file(str(HISTORY_FILE))
        except OSError:
            pass
        try:
            readline.set_completer_delims(" \t\n\"'")
            readline.set_completer(self._complete)
            readline.parse_and_bind("tab: complete")
            readline.parse_and_bind("set show-all-if-ambiguous on")
            readline.parse_and_bind("set completion-ignore-case on")
            readline.parse_and_bind("set colored-completion-prefix on")
        except Exception:
            pass
        self._rl_matches: list[str] = []
        return readline

    # ── автодополнение ────────────────────────────────────────────────
    def _complete(self, text: str, state: int):
        try:
            import readline
        except ImportError:
            return None
        if state == 0:
            buf = readline.get_line_buffer()
            beg = readline.get_begidx()
            end = readline.get_endidx()
            self._rl_matches = self._completion_candidates(buf, beg, end, text)
        if state < len(self._rl_matches):
            return self._rl_matches[state]
        return None

    def _completion_candidates(self, buf: str, beg: int, end: int, text: str) -> list[str]:
        before = buf[:beg]
        stripped = before.lstrip()
        # команда в начале строки
        if stripped == "" and (beg == 0 or before.strip() == ""):
            if text.startswith("/") or text == "":
                cmds = sorted(set(COMMANDS))
                return [c for c in cmds if c.startswith(text)]
            return []

        try:
            parts = shlex.split(before)
        except ValueError:
            parts = before.split()
        # определяем позицию аргумента
        if before.endswith(" "):
            cmd = parts[0].lower() if parts else ""
            arg_index = len(parts)  # 1-based: 1 = первый аргумент
        else:
            cmd = parts[0].lower() if parts else ""
            arg_index = max(1, len(parts))

        if not cmd.startswith("/"):
            return []

        # первый аргумент
        if arg_index == 1:
            if cmd == "/agent":
                opts = ["on", "off", "chat", "smart", "mode", "workspace", "run", "undo", "backups", "plan", "auto", "ask", "menu"]
                return [o for o in opts if o.startswith(text.lower())]
            if cmd in ("/agentchat", "/achat"):
                return [o for o in ("on", "off") if o.startswith(text.lower())]
            if cmd in ("/smart", "/auto", "/autoroute", "/умный"):
                return [o for o in ("on", "off") if o.startswith(text.lower())]
            if cmd in ("/agentmode", "/mode", "/amode"):
                return [o for o in ("ask", "auto", "plan") if o.startswith(text.lower())]
            if cmd == "/stream":
                return [o for o in ("on", "off") if o.startswith(text.lower())]
            if cmd == "/compact":
                return [o for o in ("auto", "on", "off") if o.startswith(text.lower())]
            if cmd == "/proxy":
                opts = ["on", "off", "auto", "always", "list", "add", "remove", "status"]
                return [o for o in opts if o.startswith(text.lower())]
            if cmd in ("/settings", "/config", "/cfg"):
                keys = sorted(set(self._SETTINGS_INTS) | set(self._SETTINGS_ALIASES))
                return [o for o in keys if o.startswith(text.lower())]
            if cmd in ("/debug", "/dbg"):
                return [o for o in ("--no-net",) if o.startswith(text.lower())]
            if cmd in ("/help", "/h", "/?"):
                names = sorted(set(HELP_DETAILS) | set(HELP_ALIASES))
                needle = text.lower()
                return [
                    name
                    for name in names
                    if name.startswith(needle) or name[1:].startswith(needle)
                ]
            if cmd == "/memory":
                return [o for o in ("clear",) if o.startswith(text.lower())]
            if cmd in ("/system",):
                return [o for o in ("reset",) if o.startswith(text.lower())]
            if cmd == "/model":
                opts = ["auto", "flash", "lite", "1", "2", "3"]
                return [o for o in opts if o.startswith(text.lower())]
            if cmd == "/models":
                return []
            if cmd in ("/open", "/delete", "/rm"):
                try:
                    items = self.store.list()
                except Exception:
                    items = []
                opts = []
                for i, item in enumerate(items[:20], 1):
                    opts.append(str(i))
                    opts.append(item.get("id", ""))
                return [o for o in opts if o and o.startswith(text)]
            if cmd == "/chats":
                return []
            if cmd in ("/workspace", "/ws"):
                return self._path_completions(text)
            if cmd == "/run":
                return self._path_completions(text)

        # второй аргумент
        if arg_index == 2 and cmd in ("/settings", "/config", "/cfg"):
            key = self._SETTINGS_ALIASES.get(
                (parts[1].lower() if len(parts) > 1 else ""),
                (parts[1].lower() if len(parts) > 1 else ""),
            )
            options = {
                "stream": ("on", "off"),
                "smart_route": ("on", "off"),
                "agent_chat": ("on", "off"),
                "compact_ui": ("auto", "on", "off"),
                "agent_mode": ("ask", "auto", "plan"),
                "proxy_mode": ("off", "auto", "always", "on"),
            }.get(key, ())
            return [o for o in options if o.startswith(text.lower())]

        if arg_index == 2 and cmd == "/agent":
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in ("mode", "режим"):
                return [o for o in ("ask", "auto", "plan") if o.startswith(text.lower())]
            if sub == "chat":
                return [o for o in ("on", "off") if o.startswith(text.lower())]
            if sub == "smart":
                return [o for o in ("on", "off") if o.startswith(text.lower())]
            if sub in ("workspace", "ws"):
                return self._path_completions(text)
            if sub == "run":
                return self._path_completions(text)

        # общая попытка дополнять пути
        if cmd in ("/export", "/workspace", "/ws", "/run") or (cmd == "/agent" and len(parts) >= 2 and parts[1].lower() in ("workspace", "ws", "run")):
            return self._path_completions(text)

        return []

    def _path_completions(self, text: str) -> list[str]:
        if not text:
            prefix = ""
            base_dir = "."
            show = ""
        else:
            expanded = os.path.expanduser(text)
            # split into dir part and name prefix
            if expanded.endswith(os.sep):
                base_dir = expanded
                prefix = ""
                show_base = text
            else:
                base_dir = os.path.dirname(expanded) or "."
                prefix = os.path.basename(expanded)
                show_base = os.path.dirname(text)
        try:
            entries = os.listdir(base_dir)
        except OSError:
            return []
        results = []
        for name in sorted(entries):
            if not name.startswith(prefix):
                continue
            if name.startswith(".") and not prefix.startswith("."):
                continue
            path = os.path.join(base_dir, name)
            # build completion as user typed style
            if text.endswith(os.sep):
                candidate = text + name
            elif os.path.dirname(text):
                candidate = os.path.join(os.path.dirname(text), name)
            elif text.startswith("~"):
                # os.path.dirname("~/foo") -> "~"; keep simple
                if "/" in text:
                    candidate = os.path.join(os.path.dirname(text), name)
                else:
                    candidate = "~" + os.sep + name if name else text
            else:
                candidate = name
            if os.path.isdir(path):
                candidate += os.sep
            results.append(candidate)
        return results[:40]

    def _write_history(self) -> None:
        if self._readline is None:
            return
        try:
            self._readline.write_history_file(str(HISTORY_FILE))
        except OSError:
            pass

    # ── send request ───────────────────────────────────────────────────
    def send(self, text: str) -> None:
        prompt = build_prompt(
            self.chat, self.memory.items, text, int(self.cfg["max_context_chars"])
        )
        self.last_prompt = prompt
        self.chat.add("user", text)
        self.chat.auto_title()
        self._save()
        self._generate(prompt)

    def _generate(self, prompt: str) -> None:
        selected = resolve_model(self.chat.model)
        client = Gemini(selected, timeout=float(self.cfg["timeout"]))
        self.client = client

        def _start_spinner(label: str):
            stop = threading.Event()
            frames = "◐◓◑◒"
            echo_state = None

            def _suppress_input():
                nonlocal echo_state
                if not self.tty:
                    return
                try:
                    if os.name == "nt":
                        import ctypes
                        import ctypes.wintypes as wt

                        kernel32 = ctypes.windll.kernel32
                        h = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
                        mode = wt.DWORD()
                        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                            return
                        echo_state = ("nt", h, mode.value)
                        ENABLE_ECHO_INPUT = 0x0004
                        ENABLE_LINE_INPUT = 0x0002
                        new_mode = mode.value & ~ENABLE_ECHO_INPUT & ~ENABLE_LINE_INPUT
                        kernel32.SetConsoleMode(h, new_mode)
                    else:
                        import termios

                        fd = sys.stdin.fileno()
                        old = termios.tcgetattr(fd)
                        echo_state = ("posix", fd, old)
                        new = termios.tcgetattr(fd)
                        new[3] = new[3] & ~termios.ECHO & ~termios.ICANON
                        termios.tcsetattr(fd, termios.TCSADRAIN, new)
                except Exception:
                    echo_state = None

            def _restore_input():
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

            _suppress_input()

            def run() -> None:
                i = 0
                while not stop.wait(0.12):
                    frame = frames[i % len(frames)]
                    try:
                        self.out.write("\r\033[K" + self.pal.dim(f"{frame} {label}"))
                        self.out.flush()
                    except Exception:
                        pass
                    i += 1

            t = threading.Thread(target=run, name="exelcode-spinner", daemon=True)
            t.start()

            def finish() -> None:
                stop.set()
                t.join(timeout=0.5)
                try:
                    self.out.write("\r\033[K")
                    self.out.flush()
                except Exception:
                    pass
                _restore_input()

            return finish

        route_name = selected["name"]
        _artifact_re = re.compile(
            r"(?im)^\s*https?://googleusercontent\.com/card_content/\S+\s*$\n?"
        )

        def _clean_reply(text: str) -> str:
            cleaned = _artifact_re.sub("", text or "")
            # collapse excess blank lines left behind
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
            return cleaned

        if self.stream and self.tty:
            renderer = StreamRenderer(color=self.color, compact=self.compact)
            state = {"first": True}
            stop_spinner = _start_spinner("думаю...")

            def on_delta(chunk: str) -> None:
                if state["first"]:
                    stop_spinner()
                    print()
                    state["first"] = False
                chunk = _artifact_re.sub("", chunk)
                if not chunk:
                    return
                renderer.feed(chunk)

            try:
                reply = client.ask(prompt, on_delta=on_delta)
            except KeyboardInterrupt:
                stop_spinner()
                renderer.finish()
                partial = client.last_text.strip()
                print(self.pal.yellow("sys   ⏹ stopped"))
                if partial:
                    self.chat.add("assistant", partial + "\n\n…", model=client.last_model_name)
                    self.last_answer = partial
                    self._save()
                return
            except GeminiError as exc:
                stop_spinner()
                self._error(exc)
                return
            finally:
                if state["first"]:
                    stop_spinner()
            renderer.finish()
        else:
            stop_spinner = _start_spinner("думаю...") if self.tty else (lambda: None)
            try:
                reply = client.ask(prompt)
            except KeyboardInterrupt:
                stop_spinner()
                print(self.pal.yellow("sys   ⏹ stopped"))
                return
            except GeminiError as exc:
                stop_spinner()
                self._error(exc)
                return
            stop_spinner()
            reply_text = _clean_reply(reply.text)
            print()
            if self.tty:
                print(MarkdownRenderer(color=self.color, compact=self.compact).text(reply_text))
            else:
                print(reply_text)

        reply_text = _clean_reply(reply.text)
        self.chat.add("assistant", reply_text, model=reply.model)
        self.last_answer = reply_text
        self._save()
        sep = self.pal.dim(" · ")
        if self.compact:
            meta = (
                self.pal.dim("  ")
                + self.pal.rgb(8, 103, 159, f"{reply.elapsed:.1f}s")
                + sep
                + self.pal.rgb(221, 108, 54, reply.model)
                + sep
                + self.pal.rgb(76, 175, 80, route_name)
                + sep
                + self.pal.dim("/copy")
            )
            print(meta)
        else:
            meta = (
                self.pal.dim(f"        {len(reply_text)} chars")
                + sep
                + self.pal.rgb(8, 103, 159, f"{reply.elapsed:.1f}s")
                + sep
                + self.pal.dim("model ")
                + self.pal.rgb(221, 108, 54, reply.model)
                + sep
                + self.pal.rgb(76, 175, 80, route_name)
                + sep
                + self.pal.dim("/copy")
            )
            print(meta)
        self._maybe_summarize()

    def _error(self, exc: Exception) -> None:
        self.last_error = str(exc)
        print(self.pal.red(f"agent ! error: {exc}"))
        print(self.pal.dim("sys   /retry · /model · /debug · /help"))

    def _maybe_summarize(self) -> None:
        candidates = summary_candidates(
            self.chat,
            int(self.cfg["summary_after_messages"]),
            int(self.cfg["summary_keep_recent"]),
        )
        if not candidates:
            return
        messages, new_upto = candidates
        print(self.pal.dim("sys   сжимаю старый диалог в краткий пересказ…"))
        try:
            client = Gemini("lite", timeout=float(self.cfg["timeout"]))
            reply = client.ask(make_summary_prompt(messages, self.chat.summary))
        except (GeminiError, KeyboardInterrupt):
            print(self.pal.dim("sys   не удалось обновить пересказ, попробую позже"))
            return
        self.chat.summary = reply.text.strip()
        self.chat.summary_upto = new_upto
        self._save()
        print(self.pal.dim(f"sys   пересказ обновлён: {len(messages)} сообщ."))

    # ── команды ────────────────────────────────────────────────────────
    def command(self, line: str) -> str | None:
        parts = line[1:].split(maxsplit=1)
        if not parts:
            self._help()
            return None
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            return "quit"
        if cmd in ("help", "h", "?"):
            self._help(arg)
        elif cmd == "new":
            self._new(arg)
        elif cmd in ("chats", "ls", "list"):
            self._chats()
        elif cmd in ("open", "chat"):
            self._open(arg)
        elif cmd in ("rename", "title"):
            self._rename(arg)
        elif cmd in ("delete", "rm"):
            self._delete(arg)
        elif cmd == "model":
            self._model(arg)
        elif cmd == "models":
            self._models()
        elif cmd == "memory":
            self._memory(arg)
        elif cmd in ("remember", "mem"):
            self._remember(arg)
        elif cmd == "forget":
            self._forget(arg)
        elif cmd == "system":
            self._system(arg)
        elif cmd == "retry":
            self._retry()
        elif cmd == "export":
            self._export(arg)
        elif cmd == "reset":
            self._reset(arg)
        elif cmd in ("clear", "cls"):
            if self.tty:
                self.out.write("\033[2J\033[3J\033[H")
                self.banner()
            else:
                print()
        elif cmd == "compact":
            self._compact_cmd(arg)
        elif cmd == "stats":
            self._stats()
        elif cmd == "check":
            self._check()
        elif cmd in ("debug", "dbg"):
            self._debug(arg)
        elif cmd in ("settings", "config", "cfg"):
            self._settings(arg)
        elif cmd == "proxy":
            self._proxy(arg)
        elif cmd in ("update", "upgrade", "selfupdate"):
            self._update()
        elif cmd == "agent":
            self._agent(arg)
        elif cmd in ("agentchat", "achat"):
            self._agentchat(arg)
        elif cmd in ("smart", "auto", "autoroute", "умный"):
            self._smart(arg)
        elif cmd in ("workspace", "ws"):
            self._workspace(arg)
        elif cmd in ("agentmode", "mode", "amode"):
            self._agentmode(arg)
        elif cmd == "run":
            self._run_command(arg)
        elif cmd == "do":
            self._agent(arg)
        elif cmd == "undo":
            self._undo()
        elif cmd == "backups":
            self._backups()
        elif cmd == "stream":
            self._stream(arg)
        elif cmd == "copy":
            self._copy(arg)
        elif cmd in ("about", "version"):
            self._about()
        else:
            print(self.pal.red(f"  sys  неизвестная команда: /{cmd}"))
            print(self.pal.dim("  sys  /help — список команд"))
        return None

    def _help(self, arg: str = "") -> None:
        arg = (arg or "").strip()
        if arg:
            self._help_command(arg)
            return

        print()
        print(self.pal.bold("Команды ExelCode"))
        print(self.pal.dim("  /help <команда> — подробное описание и примеры"))
        narrow = self.compact or self.width < 74
        for title, items in HELP_GROUPS:
            print()
            print("  " + self.pal.magenta(title))
            if narrow:
                for usage, short, _ in items:
                    print(f"   {self.pal.cyan(usage)}")
                    print(self.pal.dim(f"      {short}"))
            else:
                column = max(len(usage) for usage, _, _ in items)
                for usage, short, _ in items:
                    print(f"   {self.pal.cyan(usage.ljust(column))}  {short}")
        print()
        print("  " + self.pal.magenta("Сокращения"))
        by_target: dict[str, list[str]] = {}
        for alias, target in sorted(HELP_ALIASES.items()):
            by_target.setdefault(target, []).append(alias)
        for target, names in sorted(by_target.items()):
            alias_str = ", ".join(names)
            if narrow:
                print(f"   {self.pal.cyan(alias_str)}")
                print(self.pal.dim(f"      → {target}"))
            else:
                print(f"   {self.pal.cyan(alias_str.ljust(16))} → {target}")
        print()
        print(self.pal.dim("  Ввод: \\ в конце строки — продолжить на следующей."))
        print(self.pal.dim("  Ctrl-C во время ответа останавливает генерацию, в приглашении — выход."))
        print(self.pal.dim("  Tab — автодополнение команд, путей и номеров чатов."))
        print()

    def _help_command(self, arg: str) -> None:
        key = arg.split()[0].lower()
        # разрешаем писать без слэша: /help agent
        if not key.startswith("/"):
            key = "/" + key
        key = HELP_ALIASES.get(key, key)
        entry = HELP_DETAILS.get(key)
        if entry is None:
            print(self.pal.red(f"  нет такой команды: {arg}"))
            matches = [name for name in HELP_DETAILS if key[1:] in name]
            if matches:
                print(self.pal.dim("  возможно: " + ", ".join(matches[:6])))
            print(self.pal.dim("  /help — все команды по группам"))
            return
        usage, short, detail = entry
        print()
        print(f"  {self.pal.bold(self.pal.cyan(usage))} — {short}")
        print()
        for line in detail.splitlines():
            print("  " + line)
        aliases = sorted(a for a, target in HELP_ALIASES.items() if target == key)
        if aliases:
            print()
            print(self.pal.dim("  сокращения: " + ", ".join(aliases)))
        print()

    def _new(self, title: str) -> None:
        self._save()
        if self.ephemeral:
            import uuid

            self.chat = Chat(
                id="ephemeral-" + uuid.uuid4().hex[:8],
                title=title or "Новый чат",
                model=self.chat.model,
                system=self.chat.system,
            )
        else:
            self.chat = self.store.new(
                title=title or "Новый чат",
                model=self.chat.model,
                system=self.chat.system,
            )
        if not self.ephemeral:
            self.cfg["last_chat"] = self.chat.id
            save_config(self.cfg)
        self.recent_agent_files = []
        if self.tty:
            self.out.write("\033[2J\033[3J\033[H")
            self.out.flush()
            self.banner()
        print(self.pal.green(f"sys   ✓ новый чат: {self.chat.title}"))
        print(self.pal.dim(f"      id: {self.chat.id}"))

    def _chats(self) -> None:
        items = self.store.list()
        if not items:
            print(self.pal.dim("sys   чатов пока нет"))
            return
        print()
        print(self.pal.bold("Чаты"))
        for index, item in enumerate(items, 1):
            mark = self.pal.green("●") if item["id"] == self.chat.id else " "
            summary = self.pal.magenta("≡") if item["summary"] else " "
            title = item["title"][:44]
            print(
                f"  {mark} {str(index).rjust(2)}. {title.ljust(46)} "
                f"{str(item['count']).rjust(3)} сообщ.  {human_time(item['updated'])} {summary}"
            )
        print()
        print(self.pal.dim(f"  открыть: /open <номер>   папка: {CHATS_DIR}"))

    def _open(self, arg: str) -> None:
        if not arg:
            self._chats()
            print(self.pal.dim("  /open <номер|id>"))
            return
        items = self.store.list()
        chat_id = self.store.resolve(arg, items)
        if not chat_id or not self.store.exists(chat_id):
            print(self.pal.red(f"  чат не найден: {arg}"))
            return
        self._save()
        try:
            self.chat = self.store.load(chat_id)
        except (OSError, ValueError, KeyError) as exc:
            print(self.pal.red(f"  не смог прочитать чат: {exc}"))
            return
        self.cfg["last_chat"] = self.chat.id
        save_config(self.cfg)
        self.recent_agent_files = list(self.chat.recent_files)[-20:]
        if self.tty:
            self.out.write("\033[2J\033[3J\033[H")
            self.out.flush()
            self.banner()
        print(self.pal.green(f"  ✓ открыт: {self.chat.title}"))
        print(self.pal.dim(f"  {len(self.chat.messages)} сообщений · {self.chat.model}"))
        if self.chat.agent_goal:
            print(self.pal.dim(f"  агент: {self.chat.agent_goal[:80]}"))
        for msg in self.chat.messages[-2:]:
            who = "вы" if msg.role == "user" else "gemini"
            text = " ".join(msg.content.split())
            if len(text) > 160:
                text = text[:160] + "…"
            print(self.pal.dim(f"  {who}: ") + text)

    def _rename(self, arg: str) -> None:
        if not arg:
            print(self.pal.dim(f"  текущее название: {self.chat.title}"))
            print(self.pal.dim("  /rename <новое название>"))
            return
        self.chat.title = arg[:80]
        self._save()
        print(self.pal.green(f"  ✓ теперь чат называется: {self.chat.title}"))

    def _delete(self, arg: str) -> None:
        if arg.strip().lower() == "all":
            self._delete_all()
            return
        if arg:
            items = self.store.list()
            chat_id = self.store.resolve(arg, items)
        else:
            chat_id = self.chat.id
        if not chat_id or not self.store.exists(chat_id):
            print(self.pal.red("  чат не найден"))
            return
        try:
            target = self.store.load(chat_id)
        except (OSError, ValueError):
            target = None
        title = target.title if target else chat_id
        if not self._confirm(f"  удалить «{title}»?"):
            print(self.pal.dim("  отмена"))
            return
        self.store.delete(chat_id)
        print(self.pal.green(f"  ✓ удалён: {title}"))
        if chat_id == self.chat.id:
            self.chat = self.store.new(model=self.chat.model, system=self.chat.system)
            self.recent_agent_files = []
            self.cfg["last_chat"] = self.chat.id
            save_config(self.cfg)
            print(self.pal.dim(f"  открыт новый чат: {self.chat.id}"))

    def _delete_all(self) -> None:
        count = len(self.store.list())
        if not self._confirm("  удалить ВСЕ чаты и глобальную память?"):
            print(self.pal.dim("  отмена"))
            return
        deleted = 0
        for item in self.store.list():
            cid = item.get("id")
            if not cid:
                continue
            try:
                self.store.delete(cid)
                deleted += 1
            except Exception:
                pass
        self.memory.clear()
        self.chat = self.store.new(model=self.chat.model, system=self.chat.system)
        self.recent_agent_files = []
        self.cfg["last_chat"] = self.chat.id
        save_config(self.cfg)
        print(self.pal.green(f"  ✓ удалено чатов: {deleted}/{count}, память очищена"))
        print(self.pal.dim(f"  открыт новый чат: {self.chat.id}"))

    def _model(self, arg: str) -> None:
        if not arg:
            print()
            print(self.pal.bold("  Модели"))
            current = resolve_model(self.chat.model)["id"]
            for index, model in enumerate(MODELS, 1):
                mark = self.pal.green("●") if model["id"] == current else " "
                print(f"  {mark} {index}. {self.pal.cyan(model['name'])} — {model['desc']}")
            print()
            try:
                arg = self._input(self.pal.dim("  номер или id (Enter — отмена): ")).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not arg:
                return
        if arg.isdigit() and 1 <= int(arg) <= len(MODELS):
            arg = MODELS[int(arg) - 1]["id"]
        try:
            model = resolve_model(arg)
        except GeminiError as exc:
            print(self.pal.red(f"  {exc}"))
            return
        self.chat.model = model["id"]
        self.cfg["model"] = model["id"]
        save_config(self.cfg)
        self._save()
        print(self.pal.green(f"  ✓ модель: {model['name']}"))
        print(self.pal.dim("  анонимно Google обычно отвечает 3.5 Flash-Lite — фактическая модель будет в подписи"))

    def _models(self) -> None:
        current = resolve_model(self.chat.model)["id"]
        print()
        print(self.pal.bold("  Доступные маршруты"))
        for model in MODELS:
            mark = self.pal.green("●") if model["id"] == current else " "
            print(f"  {mark} {self.pal.cyan(model['id'].ljust(6))} {model['name']}")
            print(f"      {self.pal.dim(model['desc'])}")
        print()
        print(self.pal.dim("  Без ключа и cookies доступен только анонимный режим Gemini."))

    def _memory(self, arg: str) -> None:
        if arg.lower() in ("clear", "reset", "очистить"):
            if self._confirm("  очистить всю память?"):
                self.memory.clear()
                print(self.pal.green("  ✓ память очищена"))
            return
        if not self.memory.items:
            print(self.pal.dim("  память пуста"))
            print(self.pal.dim("  /remember <факт> — запомнить"))
            return
        print()
        print(self.pal.bold("  Что я помню"))
        for index, item in enumerate(self.memory.items, 1):
            print(f"  {str(index).rjust(2)}. {item['text']}")
        print()
        print(self.pal.dim(f"  файл: {MEMORY_FILE}"))

    def _remember(self, arg: str) -> None:
        if not arg:
            try:
                arg = self._input(self.pal.dim("  что запомнить? ")).strip()
            except (EOFError, KeyboardInterrupt):
                return
        if not arg:
            return
        self.memory.add(arg)
        print(self.pal.green(f"  ✓ запомнил: {arg}"))

    def _forget(self, arg: str) -> None:
        if not arg:
            self._memory("")
            print(self.pal.dim("  /forget <номер>"))
            return
        try:
            index = int(arg) - 1
        except ValueError:
            print(self.pal.red("  нужен номер из /memory"))
            return
        removed = self.memory.remove(index)
        if removed is None:
            print(self.pal.red("  нет такого номера"))
        else:
            print(self.pal.green(f"  ✓ забыл: {removed}"))

    def _system(self, arg: str) -> None:
        if not arg:
            current = self.chat.system or "(по умолчанию)"
            print(self.pal.dim(f"  роль: {current}"))
            print(self.pal.dim("  /system <текст> · /system reset"))
            return
        if arg.lower() in ("reset", "off", "сброс"):
            self.chat.system = ""
            self._save()
            print(self.pal.green("  ✓ роль сброшена"))
            return
        self.chat.system = arg
        self._save()
        print(self.pal.green("  ✓ роль обновлена"))
        print(self.pal.dim(f"  {arg[:200]}"))

    def _retry(self) -> None:
        messages = self.chat.messages
        if messages and messages[-1].role == "assistant":
            messages.pop()
        if not messages or messages[-1].role != "user":
            print(self.pal.dim("  нечего повторять"))
            return
        text = messages.pop().content
        self.chat.updated = time.time()
        self._save()
        print(self.pal.dim("  ↻ повторяю последний запрос…"))
        self.send(text)

    def _export(self, arg: str) -> None:
        if arg:
            path = Path(os.path.expanduser(arg))
        else:
            path = EXPORTS_DIR / f"{self.chat.id}.md"
        try:
            result = self.store.export_markdown(self.chat, path)
        except OSError as exc:
            print(self.pal.red(f"  не смог записать: {exc}"))
            return
        print(self.pal.green(f"  ✓ экспортировано: {result}"))

    def _reset(self, arg: str = "") -> None:
        if arg.strip().lower() == "all":
            self._reset_all()
            return
        if not self._confirm("  очистить историю этого чата?"):
            print(self.pal.dim("  отмена"))
            return
        self.chat.messages = []
        self.chat.summary = ""
        self.chat.summary_upto = 0
        self._save()
        print(self.pal.green("  ✓ история очищена (память о вас сохранена)"))

    def _reset_all(self) -> None:
        if not self._confirm(
            self.pal.red("  сбросить ВСЕ настройки/чаты/память/данные ExelCode?")
        ):
            print(self.pal.dim("  отмена"))
            return

        from .proxy import USER_FILE as PROXY_USER_FILE
        from .update import CACHE_FILE as UPDATE_CACHE_FILE

        # 1) config defaults
        self.cfg = dict(DEFAULTS)
        save_config(self.cfg)

        # 2) chats
        deleted_chats = 0
        for item in self.store.list():
            cid = item.get("id")
            if not cid:
                continue
            try:
                self.store.delete(cid)
                deleted_chats += 1
            except Exception:
                pass

        # 3) memory
        self.memory.clear()

        # 4) history file + readline
        try:
            HISTORY_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            import readline

            readline.clear_history()
        except Exception:
            pass

        # 5) directory contents
        for directory in (BACKUPS_DIR, EXPORTS_DIR):
            try:
                for child in directory.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except Exception:
                            pass
            except Exception:
                pass

        # 6) known cache/user files
        for path in (UPDATE_CACHE_FILE, PROXY_USER_FILE):
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

        # 7) runtime state
        proxy_mod.set_mode("off")
        self.stream = bool(self.cfg.get("stream", True))
        self.agent_chat = bool(self.cfg.get("agent_chat", False))
        self.smart_route = bool(self.cfg.get("smart_route", True))
        self.agent_mode = normalize_agent_mode(self.cfg.get("agent_mode", "auto"))
        self.workspace = default_workspace(self.cfg)
        self._refresh_compact()
        self.recent_agent_files = []
        self.chat = self.store.new(model=self.cfg.get("model", "auto"), system=self.cfg.get("system", ""))
        self.cfg["last_chat"] = self.chat.id
        save_config(self.cfg)
        self._save()

        if self.tty:
            self.out.write("\033[2J\033[3J\033[H")
            self.out.flush()
            self.banner()
        print(self.pal.green(f"  ✓ сброшено: чатов {deleted_chats}, память, настройки, история, кэш, прокси"))
        print(self.pal.dim(f"  открыт новый чат: {self.chat.id}"))

    def _stats(self) -> None:
        print()
        print(self.pal.bold("  Статистика"))
        print(f"  чат:      {self.chat.title}  ({self.chat.id})")
        print(f"  модель:   {self.chat.model} → {resolve_model(self.chat.model)['name']}")
        print(f"  сообщений:{len(self.chat.messages)}")
        print(f"  символов: {self.chat.chars()}")
        print(f"  пересказ: {len(self.chat.summary)} симв. (до #{self.chat.summary_upto})")
        print(f"  память:   {len(self.memory.items)} фактов")
        print(f"  агент:    {self.workspace}")
        print(f"  режим:    {AGENT_MODE_NAMES.get(self.agent_mode, self.agent_mode)}")
        if self.chat.agent_goal:
            print(f"  задача:   {self.chat.agent_goal[:70]}")
        print(f"  файлы:    {len(self.recent_agent_files)} в контексте агента")
        chat_mode = "вкл (сообщения → агенту)" if self.agent_chat else "выкл"
        print(f"  чат→агент:{chat_mode}")
        smart = "вкл (сам решает чат/действие)" if self.smart_route else "выкл"
        print(f"  умный:    {smart}")
        try:
            print(f"  откатов:  {len(list_backups(self.workspace, BACKUPS_DIR))}")
        except (AgentError, OSError):
            print("  откатов:  ?")
        print(f"  данные:   {DATA_DIR}")
        print(f"  конфиг:   {CONFIG_FILE}")
        print()

    def _check(self) -> None:
        print(self.pal.dim("  проверяю связь с Gemini…"))
        try:
            reply = Gemini("lite", timeout=float(self.cfg["timeout"])).ask(
                "Ответь ровно одним словом: ok"
            )
        except GeminiError as exc:
            print(self.pal.red(f"  ✗ {exc}"))
            return
        print(self.pal.green(f"  ✓ ответ за {reply.elapsed:.1f} с, модель {reply.model}"))

    def _debug(self, arg: str = "") -> None:
        live = "--no-net" not in (arg or "")
        print(self.pal.dim("  собираю диагностику…"))
        try:
            from .diagnostics import collect_diagnostics, format_diagnostics

            rows = collect_diagnostics(self.workspace, live=live)
            if self.last_error:
                rows.append(("Ошибки", "последняя", self.last_error[:300]))
            print()
            print(format_diagnostics(rows))
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
            print(self.pal.red(f"  ✗ диагностика не удалась: {exc}"))
        print(self.pal.dim("  без сети: /debug --no-net · JSON-вывод: --debug --json"))

    _SETTINGS_ALIASES = {
        "smart": "smart_route",
        "agentchat": "agent_chat",
        "chat_to_agent": "agent_chat",
        "mode": "agent_mode",
        "compact": "compact_ui",
       
        "proxy": "proxy_mode",
        "proxy_mode": "proxy_mode",
        "run_timeout": "agent_run_timeout",
        "run_output": "agent_run_output_chars",
        "context": "agent_context_chars",
        "diff_lines": "agent_max_diff_lines",
        "rounds": "agent_max_rounds",
        "edit_rounds": "agent_max_edit_rounds",
        "fix_rounds": "agent_max_fix_rounds",
        "summary_after": "summary_after_messages",
        "keep_recent": "summary_keep_recent",
    }
    _SETTINGS_INTS = {
        "timeout": (5, 600, "сек"),
        "max_context_chars": (1000, 200000, "симв."),
        "summary_after_messages": (4, 200, "сообщ."),
        "summary_keep_recent": (1, 100, "сообщ."),
        "agent_context_chars": (2000, 200000, "симв."),
        "agent_max_rounds": (1, 20, "раундов"),
        "agent_max_edit_rounds": (1, 20, "раундов"),
        "agent_max_fix_rounds": (0, 20, "раундов"),
        "agent_run_timeout": (5, 600, "сек"),
        "agent_run_output_chars": (500, 100000, "симв."),
        "agent_max_diff_lines": (10, 500, "строк"),
    }

    def _settings(self, arg: str = "") -> None:
        arg = (arg or "").strip()
        if not arg:
            self._print_settings()
            return
        parts = arg.split(maxsplit=1)
        key = self._SETTINGS_ALIASES.get(parts[0].lower(), parts[0].lower())
        value = parts[1].strip() if len(parts) > 1 else ""

        if key in ("model", "workspace", "agent_workspace", "last_chat"):
            hint = {
                "model": "/model <имя>",
                "workspace": "/workspace <путь>",
                "agent_workspace": "/workspace <путь>",
                "last_chat": "выбирается через /open",
            }[key]
            print(self.pal.dim(f"  {key} меняется командой: {hint}"))
            return

        if key == "stream":
            self._stream(value)
            return
        if key == "smart_route":
            self._smart(value)
            return
        if key == "agent_chat":
            self._agentchat(value)
            return
        if key == "agent_mode":
            self._agentmode(value)
            return
        if key == "proxy_mode":
            self._proxy(value or "status")
            return
        if key == "compact_ui":
            if not value:
                print(self.pal.dim(
                    f"  compact_ui: {self.cfg.get('compact_ui', 'auto')}"
                ))
                return
            if value.lower() not in ("auto", "on", "off", "вкл", "выкл"):
                print(self.pal.red("  compact_ui: auto, on или off"))
                return
            normalized = {"вкл": "on", "выкл": "off"}.get(value.lower(), value.lower())
            self.cfg["compact_ui"] = normalized
            save_config(self.cfg)
            self._refresh_compact()
            print(self.pal.green(f"  ✓ compact_ui: {normalized}"))
            return
        if key == "system":
            self._system("reset" if value.lower() in ("reset", "off", "сброс") else value)
            return
        if key in self._SETTINGS_INTS:
            low, high, unit = self._SETTINGS_INTS[key]
            if not value:
                print(self.pal.dim(f"  {key}: {self.cfg.get(key)} {unit}"))
                return
            try:
                number = int(value)
            except ValueError:
                print(self.pal.red(f"  {key}: нужно число ({low}…{high} {unit})"))
                return
            if not low <= number <= high:
                print(self.pal.red(f"  {key}: допустимо {low}…{high} {unit}"))
                return
            self.cfg[key] = number
            save_config(self.cfg)
            self._refresh_compact()
            print(self.pal.green(f"  ✓ {key}: {number} {unit}"))
            return
        print(self.pal.red(f"  неизвестная настройка: {key}"))
        print(self.pal.dim("  /settings — список ключей"))

    def _print_settings(self) -> None:
        def flag(value: bool) -> str:
            return self.pal.green("вкл") if value else self.pal.dim("выкл")

        def row(key: str, value) -> None:
            print(f"  {self.pal.cyan(key.ljust(24))} {value}")

        print()
        print(self.pal.bold("  Настройки"))
        print(self.pal.dim("  Основное"))
        row("model", f"{self.chat.model} → {resolve_model(self.chat.model)['name']}")
        row("stream", flag(self.stream))
        row("compact_ui", self.cfg.get("compact_ui", "auto") + (
            " (сейчас компактно)" if self.compact else ""
        ))
        row("timeout", f"{self.cfg['timeout']} сек")
        row("max_context_chars", self.cfg["max_context_chars"])
        row("summary_after_messages", self.cfg["summary_after_messages"])
        row("summary_keep_recent", self.cfg["summary_keep_recent"])
        row("system", (self.chat.system or "(по умолчанию)")[:60])
        print(self.pal.dim("  Агент"))
        row("agent_workspace", self.workspace)
        row("agent_mode", AGENT_MODE_NAMES.get(self.agent_mode, self.agent_mode))
        row("smart_route", flag(self.smart_route))
        row("agent_chat", flag(self.agent_chat))
        row("agent_context_chars", self.cfg["agent_context_chars"])
        row("agent_max_rounds", self.cfg["agent_max_rounds"])
        row("agent_max_edit_rounds", self.cfg["agent_max_edit_rounds"])
        row("agent_max_fix_rounds", self.cfg.get("agent_max_fix_rounds", 4))
        row("agent_run_timeout", f"{self.cfg.get('agent_run_timeout', 60)} сек")
        row("agent_run_output_chars", self.cfg.get("agent_run_output_chars", 8000))
        row("agent_max_diff_lines", self.cfg["agent_max_diff_lines"])
        print(self.pal.dim("  Прочее"))
        row("proxy_mode", self.cfg.get("proxy_mode", "auto"))
        row("last_chat", self.chat.id)
        print()
        print(self.pal.dim("  изменить: /settings <ключ> <значение> · пример: /settings timeout 90"))
        print(self.pal.dim(f"  конфиг: {CONFIG_FILE}"))
        print()

    # ── агент ──────────────────────────────────────────────────────────
    def _agent(self, arg: str) -> None:
        arg = (arg or "").strip()
        if not arg:
            self._agent_menu()
            return

        # подкоманды и одноразовые режимы
        low = arg.lower()
        if low in ("menu", "меню"):
            self._agent_menu()
            return
        if low in ("undo", "откат"):
            self._undo()
            return
        if low in ("backups", "backup", "бэкапы", "откаты"):
            self._backups()
            return
        if low in ("workspace", "ws", "папка"):
            self._workspace("")
            return
        if low.startswith("workspace ") or low.startswith("ws "):
            self._workspace(arg.split(maxsplit=1)[1].strip())
            return
        if low in ("mode", "режим"):
            self._agentmode("")
            return
        if low.startswith("mode ") or low.startswith("режим "):
            self._agentmode(arg.split(maxsplit=1)[1].strip())
            return
        if low in ("on", "chat on", "input on", "вкл"):
            self._agentchat("on")
            return
        if low in ("off", "chat off", "input off", "выкл"):
            self._agentchat("off")
            return
        if low in ("chat", "input", "чат"):
            self._agentchat("")
            return
        if low in ("smart", "smart on", "умный", "умный вкл"):
            self._smart("on" if "on" in low or "вкл" in low or low == "smart" or low == "умный" else "")
            return
        if low in ("smart off", "умный выкл"):
            self._smart("off")
            return
        if low == "run":
            self._run_command("")
            return
        if low.startswith("run "):
            self._run_command(arg.split(maxsplit=1)[1].strip())
            return

        mode = self.agent_mode
        task = arg
        for prefix, forced in (
            ("plan ", "dry-run"),
            ("dry-run ", "dry-run"),
            ("dryrun ", "dry-run"),
            ("auto ", "auto"),
            ("ask ", "ask"),
        ):
            if low.startswith(prefix):
                mode = forced
                task = arg[len(prefix):].strip()
                break

        if not task:
            self._agent_menu()
            return
        self._run_agent_task(task, mode=mode)

    def _agent_menu(self) -> None:
        print()
        print(self.pal.bold("  Агент"))
        print(f"  папка:     {self.workspace}")
        print(f"  модель:    {resolve_model(self.chat.model)['name']}")
        print(f"  режим:     {AGENT_MODE_NAMES.get(self.agent_mode, self.agent_mode)}")
        chat_mode = "вкл — обычные сообщения идут агенту" if self.agent_chat else "выкл"
        print(f"  чат→агент: {chat_mode}")
        smart = "вкл — сам решает чат или действие" if self.smart_route else "выкл"
        print(f"  умный:     {smart}")
        if self.chat.agent_goal:
            print(f"  задача:    {self.chat.agent_goal[:70]}")
        if self.recent_agent_files:
            print(f"  файлы:     {', '.join(self.recent_agent_files[-5:])}")
        print()
        options = [
            ("1", "Новая задача"),
            ("2", "Сменить папку проекта"),
            ("3", "Режим (спрашивать / авто / план)"),
            ("4", "Переключить «чат → агент» (все сообщения)"),
            ("5", "Переключить «умный режим» (сам решает)"),
            ("6", "Запустить команду в папке"),
            ("7", "Откатить последнее изменение"),
            ("8", "Точки отката"),
            ("0", "Назад"),
        ]
        for key, title in options:
            print(f"  {self.pal.cyan(key)}. {title}")
        print()
        try:
            choice = self._input(self.pal.dim("  выбор: ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("", "0", "q", "назад"):
            return
        if choice == "1":
            task = self._ask_text("  задача: ")
            if task:
                self._run_agent_task(task)
            return
        if choice == "2":
            self._workspace("")
            return
        if choice == "3":
            self._agentmode("")
            return
        if choice == "4":
            self._agentchat("toggle")
            return
        if choice == "5":
            self._smart("toggle")
            return
        if choice == "6":
            cmd = self._ask_text("  команда: ")
            if cmd:
                self._run_command(cmd)
            return
        if choice == "7":
            self._undo()
            return
        if choice == "8":
            self._backups()
            return
        print(self.pal.dim("  отмена"))

    def _ask_text(self, prompt: str) -> str:
        try:
            text = self._input(self.pal.dim(prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        return text

    def _make_agent(
        self,
        mode: str | None = None,
        history: list | None = None,
        recent_files: list[str] | None = None,
    ) -> Agent:
        mode = normalize_agent_mode(mode or self.agent_mode)
        return Agent(
            workspace=self.workspace,
            model=self.chat.model,
            timeout=float(self.cfg["timeout"]),
            context_chars=int(self.cfg["agent_context_chars"]),
            max_rounds=int(self.cfg["agent_max_rounds"]),
            max_edit_rounds=int(self.cfg["agent_max_edit_rounds"]),
            max_fix_rounds=int(self.cfg.get("agent_max_fix_rounds", 4)),
            auto_approve=(mode == "auto"),
            dry_run=(mode == "dry-run"),
            pal=self.pal,
            confirm_cb=self._confirm,
            backup_root=BACKUPS_DIR,
            max_diff_lines=int(self.cfg["agent_max_diff_lines"]),
            run_timeout=float(self.cfg.get("agent_run_timeout", 60)),
            run_output_chars=int(self.cfg.get("agent_run_output_chars", 8000)),
            history=history,
            recent_files=recent_files,
            compact=self.compact,
        )

    def _run_agent_task(self, task: str, mode: str | None = None) -> None:
        mode = normalize_agent_mode(mode or self.agent_mode)
        history = self.chat.messages[-5:]
        recent_files = list(self.recent_agent_files)
        try:
            agent = self._make_agent(mode, history=history, recent_files=recent_files)
            result = agent.run(task)
        except KeyboardInterrupt:
            print(self.pal.yellow("sys  · остановлено"))
            return
        except (AgentError, GeminiError) as exc:
            print(self.pal.red(f"sys  · ошибка: {exc}"))
            return

        self._record_agent_result(task, result, mode=mode)

        if result.error:
            print(self.pal.red(f"sys  · ошибка: {result.error}"))
        elif result.dry_run:
            print(self.pal.yellow("sys  · dry-run: файлы не изменены"))
            if result.changed:
                print(self.pal.dim(f"      план: {', '.join(result.changed)}"))
        elif result.applied or result.ran:
            if result.applied:
                print(self.pal.green(f"sys  · изменено файлов: {len(result.changed)}"))
            if result.ran:
                ok = sum(1 for r in result.ran if r.get("code") == 0)
                print(self.pal.dim(f"      проверки: {len(result.ran)}, успешно: {ok}"))
                blocked = sum(1 for r in result.ran if r.get("blocked"))
                if blocked:
                    print(self.pal.red(
                        f"      заблокировано политикой безопасности: {blocked}"
                    ))
            if result.backup:
                print(self.pal.dim(f"      откат: /undo · бэкап: {result.backup}"))
        # для обычного ответа без файловых операций ничего не показываем

    def _record_agent_result(self, task: str, result, mode: str = "") -> None:
        # track recent files for follow-ups (session/chat context)
        touched = list(getattr(result, "touched_files", []) or [])
        if not touched and getattr(result, "changed", None):
            touched = list(result.changed)
        if touched:
            merged = list(self.recent_agent_files)
            seen = set(merged)
            for f in touched:
                if f and f not in seen:
                    merged.append(f)
                    seen.add(f)
            self.recent_agent_files = merged[-20:]

        # save brief summary to shared chat history so chat/smart routing sees it
        if getattr(result, "error", "") and not getattr(result, "applied", False) and not getattr(result, "ran", None):
            # failed with nothing done - no useful context to record
            return

        # состояние агента живёт вместе с чатом (переживает перезапуск)
        self.chat.recent_files = list(self.recent_agent_files)
        self.chat.agent_goal = task.strip()[:300]
        if mode:
            self.chat.agent_mode = mode

        parts = [f'[agent task] {task.strip()[:200]}']
        changed = list(getattr(result, "changed", []) or [])
        if changed:
            parts.append("changed: " + ", ".join(changed[:10]))
        ran = list(getattr(result, "ran", []) or [])
        if ran:
            ok = sum(1 for r in ran if r.get("code") == 0)
            parts.append(f"checks: {len(ran)} (ok {ok})")
        if getattr(result, "dry_run", False):
            parts.append("mode: dry-run")
        if getattr(result, "done", False):
            parts.append("done")
        reply = (getattr(result, "reply", "") or "").strip()
        if reply and not changed and not ran:
            short = " ".join(reply.split())
            if len(short) > 300:
                short = short[:297] + "..."
            parts.append("reply: " + short)
        summary = " | ".join(parts)
        if not summary.strip():
            return
        self.chat.add("assistant", summary, model="agent")
        if reply and not changed and not ran:
            self.last_answer = reply
        else:
            self.last_answer = summary
        self._save()

    def _workspace(self, arg: str) -> None:
        arg = (arg or "").strip()
        if not arg:
            print(self.pal.dim(f"  текущая папка: {self.workspace}"))
            try:
                arg = self._input(
                    self.pal.dim("  новая папка (Enter — оставить, . — текущая): ")
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not arg:
                return
        if arg in (".", "cwd", "текущая"):
            path = Path.cwd().resolve()
        elif arg.lower() in ("reset", "default", "~"):
            path = Path.home().resolve()
        else:
            path = Path(os.path.expanduser(arg)).resolve()
        if not path.is_dir():
            print(self.pal.red(f"  папка не найдена: {path}"))
            return
        self.workspace = path
        self.cfg["agent_workspace"] = str(path)
        save_config(self.cfg)
        print(self.pal.green(f"  ✓ папка агента: {path}"))

    def _agentchat(self, arg: str) -> None:
        arg = (arg or "").strip().lower()
        if arg in ("on", "1", "yes", "вкл", "true"):
            value = True
        elif arg in ("off", "0", "no", "выкл", "false"):
            value = False
        elif arg in ("toggle", "switch"):
            value = not self.agent_chat
        elif not arg:
            state = "вкл" if self.agent_chat else "выкл"
            print(self.pal.dim(f"  чат→агент: {state}"))
            print(self.pal.dim("  /agentchat on|off — все обычные сообщения отправлять агенту"))
            print(self.pal.dim("  /smart on — умный режим: сам решает, чат это или действие"))
            return
        else:
            print(self.pal.red("  /agentchat on|off"))
            return
        self.agent_chat = value
        self.cfg["agent_chat"] = value
        if value:
            # полный режим агента важнее умной маршрутизации
            self.smart_route = False
            self.cfg["smart_route"] = False
        save_config(self.cfg)
        if value:
            print(self.pal.green("  ✓ чат→агент: вкл"))
            print(self.pal.dim("  обычные сообщения теперь выполняются агентом в папке:"))
            print(self.pal.dim(f"  {self.workspace}"))
            print(self.pal.dim("  для обычного чата с ИИ: /agentchat off"))
        else:
            print(self.pal.green("  ✓ чат→агент: выкл"))
            print(self.pal.dim("  для задач агенту используй: /agent <задача> или /smart on"))

    def _smart(self, arg: str) -> None:
        arg = (arg or "").strip().lower()
        if arg in ("on", "1", "yes", "вкл", "true"):
            value = True
        elif arg in ("off", "0", "no", "выкл", "false"):
            value = False
        elif arg in ("toggle", "switch"):
            value = not self.smart_route
        elif not arg:
            state = "вкл" if self.smart_route else "выкл"
            print(self.pal.dim(f"  умный режим: {state}"))
            print(self.pal.dim("  /smart on — сам решает: просто ответить или создать/изменить файлы"))
            print(self.pal.dim("  при действии агент работает автоматически (режим auto)"))
            return
        else:
            print(self.pal.red("  /smart on|off"))
            return
        self.smart_route = value
        self.cfg["smart_route"] = value
        if value:
            self.agent_chat = False
            self.cfg["agent_chat"] = False
            self.agent_mode = "auto"
            self.cfg["agent_mode"] = "auto"
            self.cfg["agent_auto_approve"] = True
        save_config(self.cfg)
        if value:
            print(self.pal.green("  ✓ умный режим: вкл"))
            print(self.pal.dim("  обычные вопросы — как чат; просьбы создать/изменить/запустить — агент"))
            print(self.pal.dim(f"  папка агента: {self.workspace} · режим: авто"))
            print(self.pal.dim("  можно писать без /agent: «создай file.py с hello world»"))
        else:
            print(self.pal.green("  ✓ умный режим: выкл"))
            print(self.pal.dim("  для задач агенту: /agent <задача> или /agentchat on"))

    def _agentmode(self, arg: str) -> None:
        arg = (arg or "").strip().lower()
        if not arg:
            print()
            print(self.pal.bold("  Режим агента"))
            modes = [("ask", "спрашивать перед записью/запуском файлов"), ("auto", "применять изменения и проверки автоматически"), ("dry-run", "только план и diff, без записи")]
            for index, (key, desc) in enumerate(modes, 1):
                mark = self.pal.green("●") if key == self.agent_mode else " "
                print(f"  {mark} {index}. {self.pal.cyan(key.ljust(8))} — {desc}")
            print()
            try:
                arg = self._input(self.pal.dim("  номер или режим (Enter — отмена): ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if not arg:
                return
        if arg in ("1",):
            arg = "ask"
        elif arg in ("2",):
            arg = "auto"
        elif arg in ("3", "plan"):
            arg = "dry-run"
        mode = normalize_agent_mode(arg, default="")
        if mode not in AGENT_MODE_NAMES:
            print(self.pal.red("  режим: ask, auto или plan/dry-run"))
            return
        self.agent_mode = mode
        self.cfg["agent_mode"] = mode
        # держим совместимость со старым полем
        self.cfg["agent_auto_approve"] = mode == "auto"
        save_config(self.cfg)
        print(self.pal.green(f"  ✓ режим агента: {AGENT_MODE_NAMES[mode]}"))

    def _run_command(self, cmd: str) -> None:
        cmd = (cmd or "").strip()
        if not cmd:
            cmd = self._ask_text("  команда: ")
        if not cmd:
            return
        print(self.pal.dim(f"  папка: {self.workspace}"))
        print(self.pal.cyan(f"  $ {cmd}"))
        risk = assess_command(cmd)
        for reason in risk.reasons:
            print(self.pal.yellow(f"  ⚠ {reason}"))
        if risk.dangerous:
            print(self.pal.red("  ⚠ опасная команда — подтверждайте только если понимаете последствия"))
        if not self._confirm("  выполнить команду?"):
            print(self.pal.dim("  отмена"))
            return
        code, output, elapsed = execute_command(
            cmd,
            self.workspace,
            timeout=float(self.cfg.get("agent_run_timeout", 60)),
            max_chars=int(self.cfg.get("agent_run_output_chars", 8000)),
        )
        if output:
            print()
            for line in output.splitlines()[:200]:
                color = self.pal.red if code != 0 else ""
                if color:
                    print(color("  " + line))
                else:
                    print("  " + line)
            if output.count("\n") >= 200:
                print(self.pal.dim("  …"))
        status = "ok" if code == 0 else f"exit {code}"
        mark = self.pal.green("✓") if code == 0 else self.pal.red("✗")
        print(f"\n  {mark} {status} · {elapsed:.1f} с")

    def _undo(self) -> None:
        try:
            data = undo_last(self.workspace, BACKUPS_DIR)
        except (AgentError, OSError) as exc:
            print(self.pal.red(f"  ⚠ {exc}"))
            return
        if not data:
            print(self.pal.dim("  нечего откатывать"))
            return
        restored = data.get("restored", [])
        print(self.pal.green(f"  ✓ откатано файлов: {len(restored)}"))
        for rel in restored:
            print(self.pal.dim(f"    ↩ {rel}"))

    def _backups(self) -> None:
        try:
            items = list_backups(self.workspace, BACKUPS_DIR)
        except (AgentError, OSError) as exc:
            print(self.pal.red(f"  ⚠ {exc}"))
            return
        if not items:
            print(self.pal.dim("  точек отката нет"))
            return
        print()
        print(self.pal.bold("  Точки отката"))
        for data in items[:10]:
            status = data.get("status", "?")
            stamp = time.strftime(
                "%d.%m %H:%M:%S", time.localtime(float(data.get("created_at", 0) or 0))
            )
            files = len(data.get("files", []))
            mark = {"applied": "✓", "undone": "↩", "failed": "⚠", "applying": "…"}.get(
                status, "?"
            )
            print(f"  {mark} {stamp} · {files} файл(ов) · {status}")
            print(self.pal.dim(f"      {data.get('_path')}"))
        print()

    def _stream(self, arg: str) -> None:
        value = arg.lower()
        if value in ("on", "1", "yes", "вкл"):
            self.stream = True
            self.cfg["stream"] = True
        elif value in ("off", "0", "no", "выкл"):
            self.stream = False
            self.cfg["stream"] = False
        else:
            print(self.pal.dim(f"  стриминг: {'вкл' if self.stream else 'выкл'}"))
            print(self.pal.dim("  /stream on|off"))
            return
        save_config(self.cfg)
        print(self.pal.green(f"  ✓ стриминг: {'вкл' if self.stream else 'выкл'}"))

    def _compact_cmd(self, arg: str) -> None:
        value = (arg or "").strip().lower()
        if not value:
            cur = self.cfg.get("compact_ui", "auto")
            state = "сейчас компактно" if self.compact else "сейчас обычный"
            print(self.pal.dim(f"  compact_ui: {cur} ({state})"))
            print(self.pal.dim("  /compact auto|on|off"))
            return
        if value in ("вкл", "yes", "true", "1"):
            value = "on"
        if value in ("выкл", "no", "false", "0"):
            value = "off"
        if value not in ("auto", "on", "off"):
            print(self.pal.red("  /compact auto|on|off"))
            return
        self.cfg["compact_ui"] = value
        save_config(self.cfg)
        self._refresh_compact()
        extra = " (сейчас компактно)" if self.compact else " (сейчас обычный)"
        print(self.pal.green(f"  ✓ compact_ui: {value}{extra}"))

    @staticmethod
    def _copyable_answer(text: str) -> str:
        """Для /copy: если ответ — один code block с короткой подписью, вернуть код."""
        raw = (text or "").strip()
        if not raw:
            return raw
        blocks = [m.group(1).rstrip("\n") for m in re.finditer(r"```[^\n`]*\n(.*?)```", raw, re.S)]
        if len(blocks) == 1:
            rest = re.sub(r"```[^\n`]*\n.*?```", "", raw, count=1, flags=re.S).strip()
            # короткая подпись вроде "Вот пример на Python:" — копируем сам код
            if len(rest) <= 120 and not re.search(r"[.!?](?:\s|$)", rest):
                return blocks[0].strip()
        return raw

    def _copy(self, arg: str = "") -> None:
        text = self._copyable_answer(self.last_answer or "")
        if not text:
            print(self.pal.dim("sys   нечего копировать"))
            return
        if self._copy_to_clipboard(text):
            print(self.pal.green(f"sys   ✓ скопировано в буфер ({len(text)} симв.)"))
        else:
            print(self.pal.red("sys   буфер обмена недоступен (установите wl-copy/xclip/pbcopy или используйте Windows)"))

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["clip"], input=text.encode("utf-16-le"), check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, input=text.encode("utf-8"), check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
        except Exception:
            return False
        return False

    def _proxy_notify(self, msg: str) -> None:
        try:
            print(self.pal.yellow(f"  sys  · прокси  {msg}"))
        except Exception:
            pass

    def _proxy(self, arg: str = "") -> None:
        arg = (arg or "").strip()
        if not arg or arg.lower() in ("status", "show"):
            st = proxy_mod.status()
            mode = st["mode"]
            onoff = self.pal.green(mode) if mode != "off" else self.pal.dim(mode)
            print(f"  прокси: {onoff}")
            print(self.pal.dim(f"  доступно: {st['count']} (пользовательских: {st['user_count']})"))
            print(self.pal.dim(f"  файл своих: {st['user_file']}"))
            print(self.pal.dim("  /proxy auto|on|off|always  ·  /proxy list  ·  /proxy add <host:port>"))
            return

        parts = arg.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("on", "auto", "off", "always", "вкл", "выкл"):
            try:
                mode = proxy_mod.set_mode(cmd)
            except ValueError as exc:
                print(self.pal.red(f"  {exc}"))
                return
            self.cfg["proxy_mode"] = mode
            save_config(self.cfg)
            if mode == "off":
                print(self.pal.green("  ✓ прокси: выкл"))
            else:
                print(self.pal.green(f"  ✓ прокси: {mode}"))
                print(self.pal.dim(
                    "  auto — fallback при сетевой ошибке; always — всегда через прокси"
                ))
            return

        if cmd in ("list", "ls"):
            proxies = proxy_mod.load_proxies()
            user = set(proxy_mod.user_proxies())
            if not proxies:
                print(self.pal.dim("  список прокси пуст"))
                return
            print()
            print(self.pal.bold(f"  Прокси ({len(proxies)})"))
            limit = 50
            for i, p in enumerate(proxies[:limit], 1):
                mark = self.pal.cyan("*") if p in user else " "
                print(f"  {mark} {str(i).rjust(2)}. {p}")
            if len(proxies) > limit:
                print(self.pal.dim(f"  … и ещё {len(proxies) - limit}"))
            print(self.pal.dim("  * — добавленные вами"))
            print()
            return

        if cmd == "add":
            if not rest:
                print(self.pal.red("  /proxy add <host:port>"))
                return
            try:
                p = proxy_mod.add_user_proxy(rest)
            except ValueError as exc:
                print(self.pal.red(f"  {exc}"))
                return
            print(self.pal.green(f"  ✓ добавлен: {p}"))
            print(self.pal.dim(f"  файл: {proxy_mod.USER_FILE}"))
            return

        if cmd in ("remove", "rm", "del"):
            if not rest:
                print(self.pal.red("  /proxy remove <номер|url>"))
                return
            ok = proxy_mod.remove_user_proxy(rest)
            if ok:
                print(self.pal.green(f"  ✓ удалён: {rest}"))
            else:
                print(self.pal.red("  не найден в пользовательском списке"))
            return

        print(self.pal.red("  /proxy on|off|auto|always|list|add|remove"))

    def _update(self) -> None:
        from .update import check_update_cached, install_update, restart_program

        print(self.pal.dim("sys   проверяю обновления…"))
        info = check_update_cached(timeout=12.0)
        if info.error:
            print(self.pal.red(f"sys   ✗ не удалось проверить: {info.error}"))
            return
        if not info.available:
            if info.latest and info.ahead:
                print(self.pal.green(f"sys   ✓ локальная версия новее ({info.current} > GitHub {info.latest})"))
            else:
                print(self.pal.green(f"sys   ✓ актуальная версия ({info.current})"))
                if info.latest:
                    print(self.pal.dim(f"      последняя на GitHub: {info.latest}"))
            return

        print(self.pal.yellow(f"sys   доступно обновление: {info.latest} (текущая {info.current})"))
        print(self.pal.dim(f"      {info.url}"))
        if not self._confirm("sys   Установить сейчас?"):
            print(self.pal.dim("sys   пропущено"))
            return
        print(self.pal.dim("sys   устанавливаю обновление…"))
        ok, msg = install_update(timeout=180.0)
        if ok:
            print(self.pal.green(f"sys   ✓ {msg}"))
            print(self.pal.dim("sys   перезапускаю ExelCode..."))
            restart_program()
        else:
            print(self.pal.red(f"sys   ✗ {msg}"))

    def _about(self) -> None:
        print()
        print(f"  {self.pal.bold('ExelCode')} {__version__} — Gemini, без ключа")
        print(self.pal.dim("  веб-протокол gemini.google.com, только стандартная библиотека Python"))
        print(self.pal.dim(f"  данные: {DATA_DIR}"))
        print()

    # ── служебное ──────────────────────────────────────────────────────
    def _confirm(self, question: str) -> bool:
        try:
            answer = self._input(question + " (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes", "д", "да")

    def _save(self) -> None:
        if self.ephemeral:
            return
        self.store.save(self.chat)
        self.cfg["last_chat"] = self.chat.id
        save_config(self.cfg)

    def banner(self) -> None:
        title = f" ExelCode {__version__} "
        chat = (self.chat.title or "Новый чат").strip()
        model_name = resolve_model(self.chat.model)["name"]
        model = model_name if self.chat.model == "auto" else f"{self.chat.model}→{model_name}"
        ws = str(self.workspace or "-")
        width = max(20, min(self.width, 80))

        def vlen(s: str) -> int:
            return len(_ANSI_RE.sub("", s))

        def aslice(s: str, limit: int) -> str:
            if limit <= 0:
                return ""
            out = []
            vis = 0
            i = 0
            while i < len(s) and vis < limit:
                m = _ANSI_RE.match(s, i)
                if m:
                    out.append(m.group(0))
                    i = m.end()
                    continue
                out.append(s[i])
                i += 1
                vis += 1
            return "".join(out)

        def fit(text: str, limit: int) -> str:
            limit = max(0, limit)
            if vlen(text) <= limit:
                return text
            if limit <= 1:
                return aslice(text, limit)
            return aslice(text, limit - 1) + "…"

        def ljust(text: str, limit: int, fill: str = " ") -> str:
            pad = limit - vlen(text)
            return text + (fill * pad if pad > 0 else "")

        if self.compact:
            limit = max(20, width)

            inner = max(10, limit - 2)
            title = f" ExelCode {__version__} "
            lines = [
                self.pal.dim("модель: ") + self.pal.rgb(76, 175, 80, model),
                self.pal.dim("чат: ") + self.pal.white(chat),
                self.pal.dim("папка: ") + self.pal.dim(ws),
                self.pal.rgb(106, 153, 166, "/help"),
            ]
            lines = [fit(line, inner) for line in lines]
            content_width = max([vlen(title) + 2] + [vlen(line) for line in lines])
            inner = max(10, min(inner, content_width))

            print()
            print(self.pal.cyan("╭") + self.pal.bold(self.pal.cyan(ljust("─" + title, inner, "─"))) + self.pal.cyan("╮"))
            for line in lines:
                print(self.pal.cyan("│") + ljust(fit(line, inner), inner) + self.pal.cyan("│"))
            print(self.pal.cyan("╰" + "─" * inner + "╯"))
            print()
            return

        inner = max(10, width - 2)

        def row_lr(left: str, right: str) -> str:
            right = fit(right, inner)
            space = inner - vlen(right)
            if space <= 0:
                return "│" + ljust(aslice(right, inner), inner) + "│"
            left = fit(left, space - 1 if space > 1 else 1)
            gap = inner - vlen(left) - vlen(right)
            if gap < 1:
                left = fit(left, max(0, inner - vlen(right) - 1))
                gap = inner - vlen(left) - vlen(right)
            return "│" + left + (" " * max(1, gap)) + right + "│"

        chat_part = self.pal.dim(" чат: ") + self.pal.white(fit(chat, max(1, inner // 2)))
        model_part = self.pal.dim("модель: ") + self.pal.rgb(76, 175, 80, model) + " "
        ws_part = self.pal.dim(" папка: ") + self.pal.dim(fit(ws, max(1, inner - 12)))
        help_part = self.pal.rgb(106, 153, 166, "/help ")

        top = row_lr(chat_part, model_part)
        bottom = row_lr(ws_part, help_part)
        title_line = "╭" + ljust("─" + title, inner, "─") + "╮"
        print()
        print(self.pal.cyan("╭") + self.pal.bold(self.pal.cyan(ljust("─" + title, inner, "─"))) + self.pal.cyan("╮"))
        print(self.pal.cyan("│") + top[1:-1] + self.pal.cyan("│"))
        print(self.pal.cyan("│") + bottom[1:-1] + self.pal.cyan("│"))
        print(self.pal.cyan("╰" + "─" * inner + "╯"))
        print()
