"""Smoke-тесты CLI в отдельном процессе (без сети)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def run_cli(*args: str, home: str, config: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "EXELCODE_HOME": home,
            "EXELCODE_CONFIG": config,
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "EXELCODE_COMPACT": "0",
            "EXELCODE_TERMUX": "0",
        }
    )
    return subprocess.run(
        [sys.executable, str(MAIN), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


class CliSmokeTests(unittest.TestCase):
    def test_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(
                "--version",
                home=str(Path(tmp) / "home"),
                config=str(Path(tmp) / "config.json"),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ExelCode", proc.stdout)

    def test_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(
                "--help",
                home=str(Path(tmp) / "home"),
                config=str(Path(tmp) / "config.json"),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ExelCode", proc.stdout)
        self.assertIn("--agent", proc.stdout)

    def test_empty_chat_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(
                "--list",
                home=str(Path(tmp) / "home"),
                config=str(Path(tmp) / "config.json"),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("чатов пока нет", proc.stdout)

    def test_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(
                "--models",
                home=str(Path(tmp) / "home"),
                config=str(Path(tmp) / "config.json"),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Flash-Lite", proc.stdout)


if __name__ == "__main__":
    unittest.main()
