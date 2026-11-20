"""Тесты агента без сети."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aifree.agent import (
    Agent,
    AgentError,
    FileOp,
    apply_hunks,
    apply_prepared,
    execute_command,
    format_read_key,
    normalize_agent_mode,
    parse_agent_response,
    parse_read_spec,
    prepare_ops,
    undo_last,
    Workspace,
)


class WorkspaceTests(unittest.TestCase):
    def test_read_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            (ws.root / "a.txt").write_text("привет", encoding="utf-8")
            self.assertEqual(ws.read_text("a.txt"), "привет")
            self.assertEqual(
                ws.resolve("a.txt").relative_to(ws.root).as_posix(), "a.txt"
            )

    def test_forbidden_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            for bad in ("../x", "/etc/passwd", ".env", ".git/config", "~/x"):
                with self.assertRaises(AgentError):
                    ws.resolve(bad)

    def test_tree_ignores_service_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            (ws.root / "keep.py").write_text("x", encoding="utf-8")
            (ws.root / ".git").mkdir()
            (ws.root / ".git" / "config").write_text("x", encoding="utf-8")
            (ws.root / "__pycache__").mkdir()
            (ws.root / "__pycache__" / "a.pyc").write_bytes(b"\x00")
            tree = ws.tree()
            self.assertIn("keep.py", tree)
            self.assertNotIn(".git", tree)
            self.assertNotIn("__pycache__", tree)

    def test_read_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            (ws.root / "a.txt").write_text(
                "1\n2\n3\n4\n5\n", encoding="utf-8"
            )
            self.assertEqual(ws.read_text("a.txt", start=2, end=4), "2\n3\n4\n")
            self.assertEqual(ws.read_text("a.txt:3-5"), "3\n4\n5\n")
            with self.assertRaises(AgentError):
                ws.read_text("a.txt", start=99, end=100)

    def test_read_normalizes_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            (ws.root / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
            self.assertEqual(ws.read_text("win.txt"), "one\ntwo\nthree\n")
            self.assertEqual(ws.read_text("win.txt:2-3"), "two\nthree\n")


class ParserTests(unittest.TestCase):
    def test_protocol(self):
        text = """Вот план.

READ: src/a.py
READY

```file path="hello.py"
print("hi")
```

```patch path="app.py"
<<<<<<< SEARCH
old = 1
=======
old = 2
>>>>>>> REPLACE
```

```delete path="old.py"
```

DONE
"""
        plan = parse_agent_response(text)
        self.assertEqual(plan.reads, ["src/a.py"])
        self.assertEqual(len(plan.ops), 3)
        self.assertEqual(plan.ops[0].kind, "write")
        self.assertEqual(plan.ops[0].path, "hello.py")
        self.assertEqual(plan.ops[0].content, 'print("hi")\n')
        self.assertEqual(plan.ops[1].kind, "patch")
        self.assertEqual(plan.ops[1].hunks, [("old = 1\n", "old = 2\n")])
        self.assertEqual(plan.ops[2].kind, "delete")
        self.assertTrue(plan.done)

    def test_heading_fallback(self):
        plan = parse_agent_response('### hello.py\n\n```python\nx = 1\n```\n')
        self.assertEqual(len(plan.ops), 1)
        self.assertEqual(plan.ops[0].kind, "write")
        self.assertEqual(plan.ops[0].path, "hello.py")
        self.assertEqual(plan.ops[0].content, "x = 1\n")

    def test_delete_line_and_done(self):
        plan = parse_agent_response("DELETE: old.py\nDONE\n")
        self.assertEqual(plan.ops[0].kind, "delete")
        self.assertTrue(plan.done)

    def test_patch_error(self):
        with self.assertRaises(AgentError):
            parse_agent_response('```patch path="a.py"\nнет маркеров\n```\n')

    def test_run_blocks_and_read_specs(self):
        text = (
            "READ: src/a.py:10-20\n"
            "RUN: python3 -m pytest -q\n"
            "```run\n"
            "python3 -m unittest\n"
            "```\n"
        )
        plan = parse_agent_response(text)
        self.assertEqual(plan.reads, ["src/a.py:10-20"])
        self.assertEqual(plan.runs, ["python3 -m pytest -q", "python3 -m unittest"])
        self.assertEqual(parse_read_spec("src/a.py:10-20"), ("src/a.py", 10, 20))
        self.assertEqual(parse_read_spec("src/a.py#L5-L7"), ("src/a.py", 5, 7))
        self.assertEqual(format_read_key("a.py", 1, 3), "a.py:1-3")
        self.assertEqual(normalize_agent_mode("plan"), "dry-run")
        self.assertEqual(normalize_agent_mode("auto"), "auto")
        self.assertEqual(normalize_agent_mode("ask"), "ask")


class HunkTests(unittest.TestCase):
    def test_apply_and_variants(self):
        text = "one\ntwo\nthree\n"
        hunks = [("two\n", "TWO\n")]
        self.assertEqual(apply_hunks(text, hunks, "a.py"), "one\nTWO\nthree\n")

    def test_non_unique(self):
        with self.assertRaises(AgentError):
            apply_hunks("x\nx\n", [("x\n", "y\n")], "a.py")

    def test_not_found(self):
        with self.assertRaises(AgentError):
            apply_hunks("abc\n", [("zzz\n", "x\n")], "a.py")


class ExecutorTests(unittest.TestCase):
    def test_prepare_apply_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root)
            (root / "app.py").write_text("old = 1\n", encoding="utf-8")
            (root / "gone.py").write_text("x\n", encoding="utf-8")
            ops = [
                FileOp("patch", "app.py", hunks=[("old = 1\n", "old = 2\n")]),
                FileOp("write", "new.py", content="created\n"),
                FileOp("delete", "gone.py"),
            ]
            prepared = prepare_ops(ws, ops)
            self.assertEqual({item.kind for item in prepared}, {"update", "create", "delete"})
            self.assertEqual(prepared[0].old, "old = 1\n")
            self.assertEqual(prepared[0].new, "old = 2\n")

            with tempfile.TemporaryDirectory() as backup_tmp:
                backup = apply_prepared(ws, prepared, Path(backup_tmp), task="тест")
                self.assertTrue((backup / "manifest.json").is_file())
                self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "old = 2\n")
                self.assertEqual((root / "new.py").read_text(encoding="utf-8"), "created\n")
                self.assertFalse((root / "gone.py").exists())

                data = undo_last(ws, Path(backup_tmp))
                self.assertIsNotNone(data)
                self.assertEqual(data["status"], "undone")
                self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "old = 1\n")
                self.assertFalse((root / "new.py").exists())
                self.assertEqual((root / "gone.py").read_text(encoding="utf-8"), "x\n")

    def test_patch_keeps_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root)
            (root / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
            prepared = prepare_ops(
                ws, [FileOp("patch", "win.py", hunks=[("b = 2\n", "b = 3\n")])]
            )
            with tempfile.TemporaryDirectory() as backup_tmp:
                apply_prepared(ws, prepared, Path(backup_tmp))
            self.assertEqual((root / "win.py").read_bytes(), b"a = 1\r\nb = 3\r\n")


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.replies.pop(0), model="Fake", elapsed=0.1)


class AgentTests(unittest.TestCase):
    def test_agent_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            client = FakeClient(
                [
                    "READ: a.py",
                    "READY",
                    '```file path="b.py"\nnew = 2\n```\n\nDONE\n',
                ]
            )
            agent = Agent(
                workspace=root,
                client=client,
                auto_approve=True,
                quiet=True,
                backup_root=root / "backups",
            )
            result = agent.run("создай b.py")
            self.assertTrue(result.applied)
            self.assertEqual((root / "b.py").read_text(encoding="utf-8"), "new = 2\n")
            self.assertTrue(result.backup.is_dir())
            self.assertEqual(len(client.prompts), 3)

            data = undo_last(root, root / "backups")
            self.assertIsNotNone(data)
            self.assertFalse((root / "b.py").exists())

    def test_read_range_spec_in_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
            client = FakeClient(
                [
                    "READ: a.py:2-3",
                    "READY",
                    "DONE",
                ]
            )
            agent = Agent(
                workspace=root,
                client=client,
                auto_approve=True,
                quiet=True,
                backup_root=root / "backups",
            )
            result = agent.run("посмотри середину")
            self.assertIn("a.py:2-3", client.prompts[1])
            self.assertIn("l2\nl3", client.prompts[1])

    def test_run_and_fix_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = {"n": 0}

            def runner(cmd, cwd, timeout, max_chars):
                calls["n"] += 1
                if calls["n"] == 1:
                    return 1, "FAILED test_app.py", 0.1
                return 0, "ok", 0.1

            client = FakeClient(
                [
                    "READY",
                    '```file path="app.py"\nprint("v1")\n```\n```run\npytest -q\n```',
                    '```file path="app.py"\nprint("v2")\n```\n```run\npytest -q\n```\nDONE',
                ]
            )
            agent = Agent(
                workspace=root,
                client=client,
                auto_approve=True,
                quiet=True,
                backup_root=root / "backups",
                runner=runner,
            )
            result = agent.run("почини тесты")
            self.assertTrue(result.applied)
            self.assertEqual(
                (root / "app.py").read_text(encoding="utf-8"), 'print("v2")\n'
            )
            self.assertEqual(len(result.ran), 2)
            self.assertEqual(result.ran[0]["code"], 1)
            self.assertEqual(result.ran[1]["code"], 0)
            self.assertIn("FAILED", client.prompts[2])
            self.assertIn("exit 1", client.prompts[2])

    def test_execute_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, elapsed = execute_command(
                "python3 -c \"print('hi')\"", tmp, timeout=10
            )
            self.assertEqual(code, 0)
            self.assertIn("hi", out)
            self.assertGreaterEqual(elapsed, 0.0)

    def test_execute_command_utf8_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "say.py"
            script.write_text('print("привет ✓")\n', encoding="utf-8")
            code, out, _ = execute_command("python3 say.py", tmp, timeout=20)
            self.assertEqual(code, 0)
            self.assertIn("привет ✓", out)


if __name__ == "__main__":
    unittest.main()
