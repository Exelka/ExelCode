"""Интерактивный REPL и команды приложения."""

from __future__ import annotations

import atexit
import os
import re
import shlex
import sys
import time
from pathlib import Path

from . import __version__
from .config import (
    BACKUPS_DIR,
    CHATS_DIR,
    CONFIG_FILE,
    DATA_DIR,
    EXPORTS_DIR,
    HISTORY_FILE,
    MEMORY_FILE,
    save_config,
)
from .agent import (
    Agent,
    AgentError,
    default_workspace,
    execute_command,
    is_action_request,
    list_backups,
    normalize_agent_mode,
    undo_last,
)
from .gemini import MODELS, Gemini, GeminiError, resolve_model
from .prompt import build_prompt, make_summary_prompt, summary_candidates
from .render import MarkdownRenderer, Palette, StreamRenderer, terminal_width
from .store import Chat, ChatStore, Memory, human_time

HELP = [
    ("/help", "эта справка"),
    ("/new [название]", "новый чат"),
    ("/chats", "список чатов"),
    ("/open <номер|id>", "открыть чат"),
    ("/rename <название>", "переименовать текущий чат"),
    ("/delete [номер|id]", "удалить чат"),
    ("/model [имя]", "выбрать модель"),
    ("/models", "список моделей"),
    ("/memory", "что я помню о вас"),
    ("/remember <факт>", "запомнить факт"),
    ("/forget <номер>", "забыть факт"),
    ("/system [текст]", "роль/характер ассистента"),
    ("/retry", "повторить последний ответ"),
    ("/export [файл]", "экспорт чата в markdown"),
    ("/reset", "очистить историю текущего чата"),
    ("/clear", "очистить экран"),
    ("/stats", "статистика и пути данных"),
    ("/check", "проверить связь с Gemini"),
    ("/smart on|off", "умный режим: сам решает чат или действие"),
    ("/agent [задача]", "агент: меню/задача по файлам"),
    ("/agentchat on|off", "все обычные сообщения → агенту"),
    ("/workspace [путь]", "папка проекта для агента"),
    ("/agentmode [режим]", "режим агента: ask, auto, plan"),
    ("/run <команда>", "запустить команду в папке агента"),
    ("/undo", "откатить последние изменения агента"),
    ("/backups", "точки отката агента"),
    ("/stream on|off", "стриминг ответа"),
    ("/quit", "выход"),
]

COMMANDS = [name.split()[0] for name, _ in HELP] + [
    "/h", "/?", "/exit", "/q", "/ls", "/list", "/rm", "/title", "/mem",
    "/cls", "/about", "/version", "/ws", "/amode", "/mode", "/do", "/achat",
]


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
        self.width = terminal_width()
        self.out = sys.stdout
        self.tty = sys.stdout.isatty()
        self.last_prompt = ""
        self.client: Gemini | None = None
        self._readline = self._setup_readline()

    # ── запуск ─────────────────────────────────────────────────────────
    def run(self) -> int:
        self.banner()
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
                    print(self.pal.dim("  ⚡ задача для агента — выполняю автоматически"))
                    self._run_agent_task(line)
                else:
                    self.send(line)
        finally:
            self._save()
            self._write_history()
            print(self.pal.dim("  пока-пока 👋"))
        return 0

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
            print(f"ошибка: {exc}", file=sys.stderr)
            return 1

        self.chat.add("assistant", reply.text, model=reply.model)
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
    def _read_input(self) -> str | None:
        label = "агент › " if self.agent_chat else "вы › "
        prompt = self.pal.cyan(label)
        line = input(prompt)
        if not line.endswith("\\"):
            return line
        chunks = [line[:-1]]
        while True:
            more = input(self.pal.dim("... "))
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

    # ── отправка запроса ───────────────────────────────────────────────
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
        print(self.pal.dim(f"  🤖 {selected['name']}"))
        client = Gemini(selected, timeout=float(self.cfg["timeout"]))
        self.client = client

        if self.stream and self.tty:
            renderer = StreamRenderer(self.width, color=self.color)
            state = {"first": True}

            def on_delta(chunk: str) -> None:
                if state["first"]:
                    self.out.write("\r\033[K")
                    state["first"] = False
                renderer.feed(chunk)

            self.out.write(self.pal.dim("  · думает…"))
            self.out.flush()
            try:
                reply = client.ask(prompt, on_delta=on_delta)
            except KeyboardInterrupt:
                self.out.write("\r\033[K")
                renderer.finish()
                partial = client.last_text.strip()
                print(self.pal.yellow("  ⏹ остановлено"))
                if partial:
                    self.chat.add("assistant", partial + "\n\n…", model=client.last_model_name)
                    self._save()
                return
            except GeminiError as exc:
                self.out.write("\r\033[K")
                self._error(exc)
                return
            finally:
                if state["first"]:
                    self.out.write("\r\033[K")
                    self.out.flush()
            renderer.finish()
        else:
            try:
                reply = client.ask(prompt)
            except KeyboardInterrupt:
                print(self.pal.yellow("  ⏹ остановлено"))
                return
            except GeminiError as exc:
                self._error(exc)
                return
            if self.tty:
                print(MarkdownRenderer(self.width, color=self.color).text(reply.text))
            else:
                print(reply.text)

        self.chat.add("assistant", reply.text, model=reply.model)
        self._save()
        print(
            self.pal.dim(
                f"  ─ {reply.model} · {reply.elapsed:.1f} с · {reply.chars} симв."
            )
        )
        self._maybe_summarize()

    def _error(self, exc: Exception) -> None:
        print(self.pal.red(f"  ⚠ {exc}"))
        print(self.pal.dim("  /retry — повторить · /model — сменить модель · /help"))

    def _maybe_summarize(self) -> None:
        candidates = summary_candidates(
            self.chat,
            int(self.cfg["summary_after_messages"]),
            int(self.cfg["summary_keep_recent"]),
        )
        if not candidates:
            return
        messages, new_upto = candidates
        print(self.pal.dim("  🗜  сжимаю старую часть диалога в память…"))
        try:
            client = Gemini("lite", timeout=float(self.cfg["timeout"]))
            reply = client.ask(make_summary_prompt(messages, self.chat.summary))
        except (GeminiError, KeyboardInterrupt):
            print(self.pal.dim("  не получилось — попробую позже"))
            return
        # В промпт уже передавался старый пересказ, поэтому новый его заменяет.
        self.chat.summary = reply.text.strip()
        self.chat.summary_upto = new_upto
        self._save()
        print(self.pal.dim(f"  память обновлена: {len(messages)} сообщений сжато"))

    # ── команды ────────────────────────────────────────────────────────
    def command(self, line: str) -> str | None:
        parts = line[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            return "quit"
        if cmd in ("help", "h", "?"):
            self._help()
        elif cmd == "new":
            self._new(arg)
        elif cmd in ("chats", "ls", "list"):
            self._chats()
        elif cmd == "open":
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
            self._reset()
        elif cmd in ("clear", "cls"):
            if self.tty:
                self.out.write("\033[2J\033[H")
                self.banner()
            else:
                print()
        elif cmd == "stats":
            self._stats()
        elif cmd == "check":
            self._check()
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
        elif cmd in ("about", "version"):
            self._about()
        else:
            print(self.pal.red(f"  неизвестная команда: /{cmd}"))
            print(self.pal.dim("  /help — список команд"))
        return None

    def _help(self) -> None:
        print()
        print(self.pal.bold("  Команды"))
        for name, desc in HELP:
            print(f"  {self.pal.cyan(name.ljust(20))} {desc}")
        print()
        print(self.pal.dim("  Перенос строки в запросе — обратный слэш \\ в конце."))
        print(self.pal.dim("  Ctrl-C во время ответа останавливает генерацию."))
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
        print(self.pal.green(f"  ✓ новый чат: {self.chat.title}"))
        print(self.pal.dim(f"  id: {self.chat.id}"))

    def _chats(self) -> None:
        items = self.store.list()
        if not items:
            print(self.pal.dim("  чатов пока нет"))
            return
        print()
        print(self.pal.bold("  Чаты"))
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
        print(self.pal.green(f"  ✓ открыт: {self.chat.title}"))
        print(self.pal.dim(f"  {len(self.chat.messages)} сообщений · {self.chat.model}"))
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
            self.cfg["last_chat"] = self.chat.id
            save_config(self.cfg)
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
                arg = input(self.pal.dim("  номер или id (Enter — отмена): ")).strip()
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
                arg = input(self.pal.dim("  что запомнить? ")).strip()
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

    def _reset(self) -> None:
        if not self._confirm("  очистить историю этого чата?"):
            print(self.pal.dim("  отмена"))
            return
        self.chat.messages = []
        self.chat.summary = ""
        self.chat.summary_upto = 0
        self._save()
        print(self.pal.green("  ✓ история очищена (память о вас сохранена)"))

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
            choice = input(self.pal.dim("  выбор: ")).strip().lower()
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
            text = input(self.pal.dim(prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        return text

    def _make_agent(self, mode: str | None = None) -> Agent:
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
        )

    def _run_agent_task(self, task: str, mode: str | None = None) -> None:
        mode = normalize_agent_mode(mode or self.agent_mode)
        label = AGENT_MODE_NAMES.get(mode, mode)
        print(self.pal.dim(f"  режим: {label} · папка: {self.workspace}"))
        try:
            agent = self._make_agent(mode)
            result = agent.run(task)
        except KeyboardInterrupt:
            print(self.pal.yellow("  ⏹ остановлено"))
            return
        except (AgentError, GeminiError) as exc:
            print(self.pal.red(f"  ⚠ {exc}"))
            return
        if result.error:
            print(self.pal.red(f"  ⚠ {result.error}"))
        elif result.dry_run:
            print(self.pal.yellow("  dry-run: файлы не изменены"))
            if result.changed:
                print(self.pal.dim(f"  план: {', '.join(result.changed)}"))
        elif result.applied or result.ran:
            if result.applied:
                print(self.pal.green(f"  ✓ изменено файлов: {len(result.changed)}"))
            if result.ran:
                ok = sum(1 for r in result.ran if r.get("code") == 0)
                print(self.pal.dim(f"  проверок: {len(result.ran)}, успешно: {ok}"))
            if result.backup:
                print(self.pal.dim(f"  откат: /undo · бэкап: {result.backup}"))
        else:
            print(self.pal.dim("  изменений нет"))

    def _workspace(self, arg: str) -> None:
        arg = (arg or "").strip()
        if not arg:
            print(self.pal.dim(f"  текущая папка: {self.workspace}"))
            try:
                arg = input(
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
                arg = input(self.pal.dim("  номер или режим (Enter — отмена): ")).strip().lower()
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

    def _about(self) -> None:
        print()
        print(f"  {self.pal.bold('ai-free')} {__version__} — Gemini без ключа")
        print(self.pal.dim("  веб-протокол gemini.google.com, только стандартная библиотека Python"))
        print(self.pal.dim(f"  данные: {DATA_DIR}"))
        print()

    # ── служебное ──────────────────────────────────────────────────────
    def _confirm(self, question: str) -> bool:
        try:
            answer = input(question + " (y/N): ").strip().lower()
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
        title = f" ai-free {__version__} · Gemini без ключа "
        width = min(self.width - 4, max(46, len(title) + 4))
        top = "╭" + "─" * width + "╮"
        mid = "│" + title.center(width) + "│"
        bottom = "╰" + "─" * width + "╯"
        model = resolve_model(self.chat.model)["name"]
        print()
        print(self.pal.cyan("  " + top))
        print(self.pal.cyan("  " + mid))
        print(self.pal.cyan("  " + bottom))
        print(
            self.pal.dim(
                f"  чат: {self.chat.title[:40]} · модель: {model} · /help"
            )
        )
        print()
