"""Chat and long-term memory storage (JSON, no database)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import atomic_write


def now() -> float:
    return time.time()


def human_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


@dataclass
class Message:
    role: str
    content: str
    ts: float = field(default_factory=now)
    model: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            ts=float(data.get("ts", now())),
            model=str(data.get("model", "")),
        )


@dataclass
class Chat:
    id: str
    title: str = "New chat"
    created: float = field(default_factory=now)
    updated: float = field(default_factory=now)
    model: str = "auto"
    system: str = ""
    summary: str = ""
    summary_upto: int = 0
    # Состояние агента, привязанное к чату (переживает перезапуск).
    recent_files: list[str] = field(default_factory=list)
    agent_goal: str = ""
    agent_mode: str = ""
    messages: list[Message] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Chat":
        recent = data.get("recent_files", [])
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "New chat")),
            created=float(data.get("created", now())),
            updated=float(data.get("updated", now())),
            model=str(data.get("model", "auto")),
            system=str(data.get("system", "")),
            summary=str(data.get("summary", "")),
            summary_upto=int(data.get("summary_upto", 0)),
            recent_files=[str(x) for x in recent if x] if isinstance(recent, list) else [],
            agent_goal=str(data.get("agent_goal", "")),
            agent_mode=str(data.get("agent_mode", "")),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
        )

    def add(self, role: str, content: str, model: str = "") -> Message:
        msg = Message(role=role, content=content, model=model)
        self.messages.append(msg)
        self.updated = msg.ts
        return msg

    def context_messages(self) -> list[Message]:
        return self.messages[self.summary_upto:]

    def chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def auto_title(self) -> None:
        if self.title not in ("", "Новый чат", "New chat"):
            return
        for msg in self.messages:
            if msg.role == "user":
                text = " ".join(msg.content.split())
                self.title = (text[:42] + "…") if len(text) > 42 else text
                return


class ChatStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, chat_id: str) -> Path:
        return self.dir / f"{chat_id}.json"

    def save(self, chat: Chat) -> None:
        chat.updated = now()
        atomic_write(
            self.path(chat.id),
            json.dumps(asdict(chat), ensure_ascii=False, indent=2),
        )

    def load(self, chat_id: str) -> Chat:
        data = json.loads(self.path(chat_id).read_text(encoding="utf-8"))
        return Chat.from_dict(data)

    def exists(self, chat_id: str) -> bool:
        return self.path(chat_id).is_file()

    def new(self, title: str = "New chat", model: str = "auto", system: str = "") -> Chat:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        chat_id = f"{stamp}-{uuid.uuid4().hex[:4]}"
        chat = Chat(id=chat_id, title=title, model=model, system=system)
        self.save(chat)
        return chat

    def delete(self, chat_id: str) -> None:
        try:
            self.path(chat_id).unlink()
        except FileNotFoundError:
            pass

    def list(self) -> list[dict]:
        items = []
        for file in self.dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                items.append(
                    {
                        "id": data.get("id", file.stem),
                        "title": data.get("title", "Untitled"),
                        "updated": float(data.get("updated", 0)),
                        "created": float(data.get("created", 0)),
                        "model": data.get("model", "auto"),
                        "count": len(data.get("messages", [])),
                        "summary": bool(data.get("summary")),
                    }
                )
            except (OSError, ValueError):
                continue
        items.sort(key=lambda x: x["updated"], reverse=True)
        return items

    def resolve(self, needle: str, items: list[dict] | None = None) -> str | None:
        """Resolve chat by full id, prefix, or list index."""
        needle = needle.strip()
        if not needle:
            return None
        items = items if items is not None else self.list()
        if needle.isdigit():
            index = int(needle) - 1
            if 0 <= index < len(items):
                return items[index]["id"]
        for item in items:
            if item["id"] == needle:
                return item["id"]
        matches = [item["id"] for item in items if item["id"].startswith(needle)]
        if len(matches) == 1:
            return matches[0]
        return None

    def export_markdown(self, chat: Chat, path: Path) -> Path:
        lines = [
            f"# {chat.title}",
            "",
            f"- Chat: `{chat.id}`",
            f"- Model: {chat.model}",
            f"- Created: {human_time(chat.created)}",
            f"- Updated: {human_time(chat.updated)}",
            "",
        ]
        if chat.system:
            lines += [f"> System: {chat.system}", ""]
        if chat.summary:
            lines += ["## Summary", "", chat.summary, ""]
        lines += ["## Dialog", ""]
        for msg in chat.messages:
            who = "user" if msg.role == "user" else "agent"
            stamp = human_time(msg.ts)
            lines += [f"### {who} · {stamp}", "", msg.content, ""]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path


class Memory:
    """Long-term facts about the user."""

    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.items = [
                    {"text": str(x.get("text", "")), "ts": float(x.get("ts", now()))}
                    for x in data
                    if isinstance(x, dict) and x.get("text")
                ]
        except (OSError, ValueError):
            self.items = []

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.items, ensure_ascii=False, indent=2))

    def add(self, text: str) -> None:
        text = " ".join(text.split()).strip()
        if not text:
            return
        if any(item["text"].lower() == text.lower() for item in self.items):
            return
        self.items.append({"text": text, "ts": now()})
        self.save()

    def remove(self, index: int) -> str | None:
        if 0 <= index < len(self.items):
            removed = self.items.pop(index)
            self.save()
            return removed["text"]
        return None

    def clear(self) -> None:
        self.items = []
        self.save()
