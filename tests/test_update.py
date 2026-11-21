"""Тесты кэша и фоновой проверки обновлений (без сети)."""

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

    def test_cache_avoids_second_network_call(self):
        calls = []

        def fake(timeout):
            calls.append(timeout)
            return UpdateInfo(current="1.0.0", latest="9.9.9", url="https://example")

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "update.json"
            first = check_update_cached(ttl=3600, cache_file=cache, checker=fake)
            second = check_update_cached(ttl=3600, cache_file=cache, checker=fake)
            forced = check_update_cached(
                ttl=3600, force=True, cache_file=cache, checker=fake
            )

            self.assertEqual(first.latest, "9.9.9")
            self.assertFalse(first.cached)
            self.assertTrue(second.cached)
            self.assertEqual(second.latest, "9.9.9")
            self.assertFalse(forced.cached)
            self.assertEqual(len(calls), 2)

            loaded = load_cached_update(cache)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.latest, "9.9.9")

    def test_expired_cache_is_refreshed(self):
        calls = []

        def fake(timeout):
            calls.append(timeout)
            return UpdateInfo(current="1.0.0", latest="2.0.0")

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "update.json"
            old = UpdateInfo(current="1.0.0", latest="1.0.0")
            save_cached_update(old, cache)
            # состарим кэш
            import json

            data = json.loads(cache.read_text(encoding="utf-8"))
            data["checked_at"] = time.time() - 10 * 3600
            cache.write_text(json.dumps(data), encoding="utf-8")

            info = check_update_cached(ttl=3600, cache_file=cache, checker=fake)
            self.assertEqual(info.latest, "2.0.0")
            self.assertEqual(len(calls), 1)

    def test_background_result_is_taken_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "update.json"
            save_cached_update(
                UpdateInfo(current="1.0.0", latest="9.9.9", url="https://example"),
                cache,
            )
            from aifree.update import start_background_update_check

            started = start_background_update_check(ttl=3600, cache_file=cache)
            self.assertTrue(started)
            info = None
            for _ in range(100):
                info = take_background_update()
                if info is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(info)
            self.assertEqual(info.latest, "9.9.9")
            self.assertIsNone(take_background_update())

    def test_background_force_ignores_fresh_cache(self):
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

            started = start_background_update_check(
                ttl=3600, cache_file=cache, force=True, checker=fake
            )
            self.assertTrue(started)
            info = None
            for _ in range(100):
                info = take_background_update()
                if info is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(info)
            self.assertEqual(info.latest, "2.0.0")
            self.assertFalse(info.cached)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
