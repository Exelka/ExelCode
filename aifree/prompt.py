"""Prompt assembly: role, memory, summary, history, question."""

from __future__ import annotations

from .store import Chat, Message

DEFAULT_SYSTEM = (
    "You are a helpful AI assistant. Answer clearly and concisely. "
    "If code is needed, provide working code with a short explanation. "
    "Do not invent facts; if you are unsure, say so."
)


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_prompt(
    chat: Chat,
    memory: list[dict],
    user_text: str,
    max_chars: int = 14000,
) -> str:
    system = (chat.system or DEFAULT_SYSTEM).strip()
    parts = [f"<system>\n{system}\n</system>"]

    if memory:
        facts = "\n".join(f"- {item['text']}" for item in memory[-40:])
        parts.append(f"<memory>\n{facts}\n</memory>")

    if chat.summary:
        parts.append(f"<summary>\n{chat.summary.strip()}\n</summary>")

    head = "\n\n".join(parts)
    tail = f"<user>\n{user_text}\n</user>"
    budget = max(1200, max_chars - len(head) - len(tail) - 300)

    selected: list[str] = []
    used = 0
    for msg in reversed(chat.context_messages()):
        who = "User" if msg.role == "user" else "Assistant"
        block = f"{who}: {msg.content.strip()}"
        if used + len(block) + 2 > budget:
            if not selected:
                block = _clip(block, budget)
            else:
                break
        selected.append(block)
        used += len(block) + 2

    selected.reverse()
    if selected:
        parts.append("<dialogue>\n" + "\n\n".join(selected) + "\n</dialogue>")

    parts.append(tail)
    parts.append(
        "Reply to the user's latest message. "
        "Do not mention the internal tags <system>, <memory>, <summary>, <dialogue>."
    )
    return "\n\n".join(parts)


def summary_candidates(
    chat: Chat,
    after_messages: int = 16,
    keep_recent: int = 6,
) -> tuple[list[Message], int] | None:
    """Return messages to summarize and the new summary_upto."""
    pending = chat.messages[chat.summary_upto:]
    if len(pending) <= after_messages:
        return None
    cut = len(chat.messages) - keep_recent
    if cut <= chat.summary_upto:
        return None
    return chat.messages[chat.summary_upto:cut], cut


def make_summary_prompt(messages: list[Message], previous: str = "") -> str:
    transcript = []
    for msg in messages:
        who = "User" if msg.role == "user" else "Assistant"
        transcript.append(f"{who}: {msg.content.strip()}")
    body = "\n\n".join(transcript)
    if len(body) > 18000:
        body = body[-18000:]
    prev = f"Previous summary:\n{previous.strip()}\n\n" if previous else ""
    return (
        "Summarize this dialog fragment briefly.\n"
        "Keep: user facts, goals, agreements, important details, decisions, and open questions.\n"
        "Write 5-10 bullet points, one per line, no preamble.\n\n"
        f"{prev}Fragment:\n{body}\n\nSummary:"
    )
