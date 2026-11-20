"""Argument parsing and application entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import __version__
from .config import BACKUPS_DIR, DATA_DIR, ensure_dirs, load_config, save_config
from .console import setup_console
from .gemini import MODELS, Gemini, GeminiError, resolve_model
from .render import Palette
from .store import Chat, ChatStore, Memory, human_time
from .ui import App


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exelcode",
        description="ExelCode — console AI chat and coding agent on Gemini (no key).",
        epilog="Without arguments, starts interactive chat.",
    )
    parser.add_argument("prompt", nargs="*", help="one-shot prompt (no REPL)")
    parser.add_argument("-p", "--prompt", dest="prompt_opt", help="one-shot prompt")
    parser.add_argument("-m", "--model", help="model: auto, flash, lite")
    parser.add_argument("--new", action="store_true", help="start a new chat")
    parser.add_argument("--chat", help="open chat by index or id")
    parser.add_argument("--list", action="store_true", help="list chats")
    parser.add_argument("--models", action="store_true", help="list models")
    parser.add_argument("--no-stream", action="store_true", help="disable streaming")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    parser.add_argument("--ephemeral", action="store_true", help="do not save chat")
    parser.add_argument("--json", action="store_true", help="JSON output (with -p)")
    parser.add_argument("--check", action="store_true", help="check Gemini connectivity")
    parser.add_argument("--agent", action="store_true", help="agent mode: read and edit files")
    parser.add_argument("--workspace", help="project directory for the agent")
    parser.add_argument("--agent-mode", choices=["ask", "auto", "plan"], help="agent mode")
    parser.add_argument("-y", "--yes", action="store_true", help="apply changes/checks without prompt")
    parser.add_argument("--dry-run", action="store_true", help="show plan and diff, do not apply")
    parser.add_argument("--undo", action="store_true", help="undo last agent changes")
    parser.add_argument("--backups", action="store_true", help="list agent restore points")
    parser.add_argument("--update", action="store_true", help="check for updates and install")
    parser.add_argument("--version", action="store_true", help="show version")
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"ExelCode {__version__}")
        return 0

    ensure_dirs()
    cfg = load_config()
    store = ChatStore(DATA_DIR / "chats")
    memory = Memory(DATA_DIR / "memory.json")
    color = bool(sys.stdout.isatty()) and not args.no_color and not os.environ.get("NO_COLOR")
    pal = Palette(color)

    if args.workspace:
        workspace = Path(os.path.expanduser(args.workspace)).resolve()
        if not workspace.is_dir():
            print(f"error: directory not found: {workspace}", file=sys.stderr)
            return 2
        cfg["agent_workspace"] = str(workspace)
        save_config(cfg)

    if args.undo:
        return _undo(cfg, pal)
    if args.backups:
        return _print_backups(cfg, pal)
    if args.update:
        return _cmd_update(pal, auto_yes=bool(args.yes))
    if (args.yes or args.dry_run) and not args.agent and not args.update:
        print("error: --yes and --dry-run require --agent (or --update)", file=sys.stderr)
        return 2

    if args.list:
        return _print_chats(store, cfg, pal)
    if args.models:
        return _print_models(pal)
    if args.check:
        return _check(pal, float(cfg["timeout"]))

    if args.model and args.model.isdigit() and 1 <= int(args.model) <= len(MODELS):
        args.model = MODELS[int(args.model) - 1]["id"]

    try:
        if args.model:
            cfg["model"] = resolve_model(args.model)["id"]
            save_config(cfg)
    except GeminiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = args.prompt_opt or " ".join(args.prompt).strip()
    if args.agent_mode:
        cfg["agent_mode"] = "dry-run" if args.agent_mode == "plan" else args.agent_mode
        cfg["agent_auto_approve"] = cfg["agent_mode"] == "auto"
        save_config(cfg)
    if args.yes:
        cfg["agent_mode"] = "auto"
        cfg["agent_auto_approve"] = True
        save_config(cfg)
    if args.dry_run:
        cfg["agent_mode"] = "dry-run"
        cfg["agent_auto_approve"] = False
        save_config(cfg)

    if args.agent:
        if text:
            return _run_agent(args, cfg, text, pal)
        print(pal.dim("  agent: /agent in chat · /agent <task> · /workspace · /undo"))

    try:
        chat = _pick_chat(args, cfg, store, pal)
    except GeminiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if chat.model not in {m["id"] for m in MODELS}:
        chat.model = "auto"

    app = App(
        store=store,
        memory=memory,
        cfg=cfg,
        chat=chat,
        stream=not args.no_stream and bool(cfg.get("stream", True)),
        color=color,
        ephemeral=args.ephemeral,
        agent_auto_approve=bool(args.yes),
        agent_dry_run=bool(args.dry_run),
        agent_mode=cfg.get("agent_mode"),
    )

    if text:
        return app.one_shot(text, as_json=args.json)
    return app.run()


def _pick_chat(args, cfg: dict, store: ChatStore, pal: Palette) -> Chat:
    if args.chat:
        items = store.list()
        chat_id = store.resolve(args.chat, items)
        if not chat_id or not store.exists(chat_id):
            raise GeminiError(f"chat not found: {args.chat}")
        try:
            chat = store.load(chat_id)
        except (OSError, ValueError, KeyError) as exc:
            raise GeminiError(f"could not read chat {chat_id}: {exc}") from exc
    elif args.new or args.ephemeral:
        chat = _new_chat(store, cfg, args.ephemeral)
    elif cfg.get("last_chat") and store.exists(str(cfg["last_chat"])):
        try:
            chat = store.load(str(cfg["last_chat"]))
        except (OSError, ValueError, KeyError):
            chat = _new_chat(store, cfg, args.ephemeral)
    else:
        chat = _new_chat(store, cfg, args.ephemeral)

    if args.model:
        chat.model = resolve_model(args.model)["id"]
    if not args.ephemeral:
        cfg["last_chat"] = chat.id
        save_config(cfg)
    return chat


def _new_chat(store: ChatStore, cfg: dict, ephemeral: bool) -> Chat:
    model = cfg.get("model", "auto")
    system = cfg.get("system", "")
    if ephemeral:
        return Chat(
            id="ephemeral-" + uuid.uuid4().hex[:8],
            title="New chat",
            model=model,
            system=system,
        )
    return store.new(model=model, system=system)


def _print_chats(store: ChatStore, cfg: dict, pal: Palette) -> int:
    items = store.list()
    if not items:
        print("no chats yet")
        return 0
    current = cfg.get("last_chat")
    print("Chats:")
    for index, item in enumerate(items, 1):
        mark = "*" if item["id"] == current else " "
        summary = " [summary]" if item["summary"] else ""
        print(
            f" {mark} {index:2d}. {item['title']}  "
            f"({item['count']} msgs, {item['model']}, {human_time(item['updated'])}){summary}"
        )
        print(pal.dim(f"      {item['id']}"))
    return 0


def _print_models(pal: Palette) -> int:
    print("Models:")
    for model in MODELS:
        print(f"  {model['id']:<6} {model['name']} — {model['desc']}")
    print(pal.dim("\nAnonymous mode: Google usually answers as 3.5 Flash-Lite."))
    return 0


def _check(pal: Palette, timeout: float) -> int:
    print("Checking Gemini connectivity…")
    try:
        reply = Gemini("lite", timeout=timeout).ask("Reply with exactly one word: ok")
    except GeminiError as exc:
        print(pal.red(f"  ✗ {exc}"))
        return 1
    print(pal.green(f"  ✓ reply in {reply.elapsed:.1f} s, model {reply.model}"))
    print(pal.dim(f"  {reply.text[:120]}"))
    return 0


def _run_agent(args, cfg: dict, text: str, pal: Palette) -> int:
    from .agent import Agent, AgentError, default_workspace, normalize_agent_mode

    mode = normalize_agent_mode(cfg.get("agent_mode", "ask"))
    if args.yes:
        mode = "auto"
    if args.dry_run or args.agent_mode == "plan":
        mode = "dry-run"
    auto = mode == "auto"
    dry = mode == "dry-run"
    if not auto and not sys.stdin.isatty():
        dry = True
    try:
        agent = Agent(
            workspace=default_workspace(cfg),
            model=cfg["model"],
            timeout=float(cfg["timeout"]),
            context_chars=int(cfg["agent_context_chars"]),
            max_rounds=int(cfg["agent_max_rounds"]),
            max_edit_rounds=int(cfg["agent_max_edit_rounds"]),
            max_fix_rounds=int(cfg.get("agent_max_fix_rounds", 4)),
            auto_approve=auto,
            dry_run=dry,
            pal=pal,
            backup_root=BACKUPS_DIR,
            max_diff_lines=int(cfg["agent_max_diff_lines"]),
            run_timeout=float(cfg.get("agent_run_timeout", 60)),
            run_output_chars=int(cfg.get("agent_run_output_chars", 8000)),
            quiet=args.json,
        )
        result = agent.run(text)
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    except (AgentError, GeminiError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(pal.red(f"error: {exc}"), file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "applied": result.applied,
                    "dry_run": result.dry_run,
                    "changed": result.changed,
                    "ran": result.ran,
                    "backup": str(result.backup) if result.backup else None,
                    "rounds": result.rounds,
                    "error": result.error,
                    "done": result.done,
                },
                ensure_ascii=False,
            )
        )
    elif result.error:
        print(pal.red(f"error: {result.error}"))
    elif result.dry_run:
        print(pal.yellow("dry-run: files not changed"))
        if result.changed:
            print(pal.dim("plan: " + ", ".join(result.changed)))
    elif result.applied or result.ran:
        if result.applied:
            print(pal.green(f"done: changed {len(result.changed)} file(s)"))
        if result.ran:
            ok = sum(1 for r in result.ran if r.get("code") == 0)
            print(pal.dim(f"checks: {len(result.ran)}, ok: {ok}"))
        if result.backup:
            print(pal.dim(f"undo: --undo · backup: {result.backup}"))
    else:
        print(pal.dim("no changes"))
    return 0 if not result.error else 1


def _undo(cfg: dict, pal: Palette) -> int:
    from .agent import AgentError, default_workspace, undo_last

    try:
        data = undo_last(default_workspace(cfg), BACKUPS_DIR)
    except AgentError as exc:
        print(pal.red(f"error: {exc}"))
        return 1
    if not data:
        print("nothing to undo")
        return 0
    restored = data.get("restored", [])
    print(pal.green(f"restored files: {len(restored)}"))
    for rel in restored:
        print(pal.dim(f"  ↩ {rel}"))
    return 0


def _print_backups(cfg: dict, pal: Palette) -> int:
    from .agent import AgentError, default_workspace, list_backups

    try:
        workspace = default_workspace(cfg)
        items = list_backups(workspace, BACKUPS_DIR)
    except AgentError as exc:
        print(pal.red(f"error: {exc}"))
        return 1
    if not items:
        print("no restore points")
        return 0
    print(f"Restore points · {workspace}")
    for data in items[:10]:
        status = data.get("status", "?")
        stamp = time.strftime(
            "%d.%m %H:%M:%S", time.localtime(float(data.get("created_at", 0) or 0))
        )
        files = len(data.get("files", []))
        mark = {"applied": "✓", "undone": "↩", "failed": "⚠", "applying": "…"}.get(
            status, "?"
        )
        print(f" {mark} {stamp} · {files} file(s) · {status}")
        print(pal.dim(f"      {data.get('_path')}"))
    return 0


def _cmd_update(pal: Palette, auto_yes: bool = False) -> int:
    from .update import check_update, install_update

    print("Checking for updates…")
    info = check_update(timeout=12.0)
    if info.error:
        print(pal.red(f"  ✗ could not check: {info.error}"))
        return 1
    if not info.available:
        if info.latest and info.ahead:
            print(pal.green(f"  ✓ local version is ahead ({info.current} > GitHub {info.latest})"))
        else:
            print(pal.green(f"  ✓ up to date ({info.current})"))
            if info.latest:
                print(pal.dim(f"    latest on GitHub: {info.latest}"))
        return 0

    print(pal.yellow(f"  update available: {info.latest} (current {info.current})"))
    print(pal.dim(f"  {info.url}"))

    do_install = auto_yes
    if not do_install and sys.stdin.isatty():
        try:
            answer = input("  Install now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        do_install = answer in ("y", "yes")
    elif not do_install:
        print(pal.dim("  run interactively or with --update -y to install"))
        return 0

    if not do_install:
        print(pal.dim("  skipped"))
        return 0

    print("Installing update…")
    ok, msg = install_update(timeout=180.0)
    if ok:
        print(pal.green(f"  ✓ {msg}"))
        print(pal.dim("  restart ExelCode to use the new version"))
        return 0
    print(pal.red(f"  ✗ {msg}"))
    return 1
