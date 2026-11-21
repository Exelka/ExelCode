"""Тесты проверки обновлений (без сети)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from aifree.update import (
    UpdateInfo,
    check_update_cached,
    load_cached_update,
    save_cached_update,
    take_background_update,
    version_tuple,
)


class UpdateCacheTests(unittest.TestCase):
    def test_version_order(self):
        self.assertGreater(version_tuple("1.6.0"), version_tuple("1.5.9"))
        self.assertGreater(version_tuple("2.0.0"), version_tuple("1.99.99"))

    def test_always_checks_no_cooldown(self):
        calls = []

        def fake(timeout):
            calls.append(timeout)
            return UpdateInfo(current="1.0.0", latest="9.9.9", url="https://example")

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "update.json"
            first = check_update_cached(cache_file=cache, checker=fake)
            second = check_update_cached(cache_file=cache, checker=fake)
            third = check_update_cached(cache_file=cache, checker=fake)

            self.assertEqual(first.latest, "9.9.9")
            self.assertEqual(second.latest, "9.9.9")
            self.assertEqual(third.latest, "9.9.9")
            self.assertEqual(len(calls), 3)  # каждый запуск = реальная проверка

            loaded = load_cached_update(cache)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.latest, "9.9.9")

    def test_background_check_always_runs(self):
        calls = []

        def fake(timeout):
            calls.append(timeout)
            return UpdateInfo(current="1.0.0", latest="2.0.0", url="https://example")

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "update.json"
            save_cached_update(
                UpdateInfo(current="1.0.0", latest="1.0.0", url="https://example"),
                cache,
            )
            from aifree.update import start_background_update_check

            started = start_background_update_check(cache_file=cache, checker=fake)
            self.assertTrue(started)
            info = None
            for _ in range(100):
                info = take_background_update()
                if info is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(info)
            self.assertEqual(info.latest, "2.0.0")
            self.assertEqual(len(calls), 1)
            self.assertIsNone(take_background_update())


if __name__ == "__main__":
    unittest.main()
