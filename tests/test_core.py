"""Быстрые тесты без сети: python3 -m unittest discover -s tests"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from aifree.console import IS_WINDOWS, adapt_command, decode_output, normalize_newlines
from aifree.gemini import GeminiError, is_failure_reply, resolve_model
from aifree.highlight import highlight_line, new_state
from aifree.prompt import build_prompt, make_summary_prompt, summary_candidates
from aifree.render import MarkdownRenderer, StreamRenderer, strip_inline
from aifree.store import Chat, ChatStore, Memory, Message


class StoreTests(unittest.TestCase):
    def test_chat_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(Path(tmp))
            chat = store.new(title="Тест", model="lite")
            chat.add("user", "привет")
            chat.add("assistant", "здравствуйте", model="3.5 Flash-Lite")
            store.save(chat)

            loaded = store.load(chat.id)
            self.assertEqual(loaded.title, "Тест")
            self.assertEqual(len(loaded.messages), 2)
            self.assertEqual(loaded.messages[1].model, "3.5 Flash-Lite")
            self.assertEqual(store.list()[0]["count"], 2)

    def test_memory_dedup_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "memory.json")
            memory.add("Люблю краткость")
            memory.add("Люблю краткость")
            memory.add("Зовут Алексей")
            self.assertEqual(len(memory.items), 2)
            self.assertEqual(memory.remove(0), "Люблю краткость")
            self.assertEqual(len(memory.items), 1)

    def test_resolve_by_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(Path(tmp))
            chat = store.new(title="Один")
            self.assertEqual(store.resolve(chat.id[:10]), chat.id)
            self.assertEqual(store.resolve("1"), chat.id)


class PromptTests(unittest.TestCase):
    def test_blocks_and_budget(self):
        chat = Chat(id="x", summary="Краткий пересказ", summary_upto=1)
        chat.messages = [
            Message("user", "старое"),
            Message("assistant", "ответ"),
            Message("user", "ещё вопрос"),
        ]
        prompt = build_prompt(chat, [{"text": "Зовут Алексей"}], "новый вопрос", 3000)
        for tag in ("<system>", "<memory>", "<summary>", "<dialogue>", "<user>"):
            self.assertIn(tag, prompt)
        self.assertIn("Зовут Алексей", prompt)
        self.assertIn("новый вопрос", prompt)

    def test_summary_candidates(self):
        chat = Chat(id="x")
        for i in range(20):
            chat.messages.append(Message("user" if i % 2 == 0 else "assistant", f"m{i}"))
        result = summary_candidates(chat, after_messages=16, keep_recent=6)
        self.assertIsNotNone(result)
        messages, upto = result
        self.assertEqual(upto, 14)
        self.assertEqual(len(messages), 14)
        self.assertIn("Fragment", make_summary_prompt(messages))


class GeminiTests(unittest.TestCase):
    def test_models(self):
        self.assertEqual(resolve_model("3.5")["id"], "lite")
        self.assertEqual(resolve_model("flash")["id"], "flash")
        with self.assertRaises(GeminiError):
            resolve_model("нет-такой")

    def test_failure_detection(self):
        self.assertTrue(
            is_failure_reply("I encountered an error doing what you asked. Try again?")
        )
        self.assertFalse(is_failure_reply("Something went wrong in your code because x"))


class RenderTests(unittest.TestCase):
    def test_inline_keeps_code_names(self):
        self.assertEqual(strip_inline("my_var_name"), "my_var_name")
        self.assertEqual(strip_inline("2*3*4"), "2*3*4")
        self.assertEqual(strip_inline("**жирный**"), "жирный")
        self.assertEqual(strip_inline("[x](https://y)"), "x (https://y)")

    def test_highlight_python(self):
        painted = highlight_line("def f(x):  # привет", "python")
        self.assertIn("\033[1;35mdef", painted)
        self.assertIn("\033[90m# привет", painted)

    def test_highlight_disabled_is_plain(self):
        line = "const x = 'a' // c"
        self.assertEqual(highlight_line(line, "js", enabled=False), line)

    def test_highlight_block_comment_state(self):
        state = new_state()
        first = highlight_line("/* начало", "c", state)
        second = highlight_line("конец */ int x = 1;", "c", state)
        self.assertIn("\033[90m/* начало", first)
        self.assertTrue(second.startswith("\033[90mконец */"))
        self.assertIn("\033[1;35mint", second)

    def test_highlight_json_and_diff(self):
        obj = highlight_line('{"key": "value", "n": 3}', "json")
        self.assertIn("\033[36m\"key\"", obj)
        self.assertIn("\033[32m\"value\"", obj)
        self.assertIn("\033[33m3", obj)
        self.assertIn("\033[32m+ new", highlight_line("+ new", "diff"))
        self.assertIn("\033[31m- old", highlight_line("- old", "diff"))

    def test_markdown_code_block_no_ansi_when_plain(self):
        text = "```python\nprint('hi')\n```"
        out = MarkdownRenderer(width=60, color=False).text(text)
        self.assertNotIn("\033[", out)
        self.assertIn("print('hi')", out)

    def test_stream_renderer_code_block(self):
        buffer = io.StringIO()
        renderer = StreamRenderer(width=60, color=True, out=buffer)
        for chunk in ("```python\n", "def f():\n", "    return 1\n", "```\n"):
            renderer.feed(chunk)
        renderer.finish()
        result = buffer.getvalue()
        self.assertIn("\033[1;35mdef", result)
        self.assertIn("python", result)


class ConsoleTests(unittest.TestCase):
    def test_normalize_newlines(self):
        self.assertEqual(normalize_newlines("a\r\nb\rc\nd"), "a\nb\nc\nd")
        self.assertEqual(normalize_newlines("без переносов"), "без переносов")

    def test_decode_output(self):
        self.assertEqual(decode_output("привет ✓".encode("utf-8")), "привет ✓")
        self.assertEqual(decode_output(b""), "")

    def test_adapt_command_plain_command(self):
        self.assertEqual(adapt_command("pytest -q"), "pytest -q")

    def test_adapt_command_python3(self):
        result = adapt_command("python3 -m pytest -q")
        self.assertTrue(result.endswith("-m pytest -q"))
        self.assertEqual(result == "python3 -m pytest -q", not IS_WINDOWS)


if __name__ == "__main__":
    unittest.main()
