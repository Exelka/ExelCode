"""Тесты прокси-модуля (без реальной сети)."""

from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import aifree.proxy as proxy_mod


class ProxyModuleTests(unittest.TestCase):
    def setUp(self):
        self._orig_mode = proxy_mod.get_mode()
        self._orig_dead = dict(proxy_mod._STATE.get("dead_until", {}))
        proxy_mod.set_mode("off")
        proxy_mod._STATE["dead_until"] = {}

    def tearDown(self):
        proxy_mod.set_mode(self._orig_mode)
        proxy_mod._STATE["dead_until"] = self._orig_dead

    def test_normalize(self):
        self.assertEqual(proxy_mod.normalize_proxy("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(proxy_mod.normalize_proxy("http://1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertIsNone(proxy_mod.normalize_proxy(""))
        self.assertIsNone(proxy_mod.normalize_proxy("ftp://1.2.3.4:21"))
        self.assertIsNone(proxy_mod.normalize_proxy("http://noport"))

    def test_add_remove_user_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_file = Path(tmp) / "proxies.txt"
            with patch.object(proxy_mod, "USER_FILE", user_file):
                added = proxy_mod.add_user_proxy("5.6.7.8:3128")
                self.assertEqual(added, "http://5.6.7.8:3128")
                self.assertEqual(proxy_mod.user_proxies(), ["http://5.6.7.8:3128"])
                self.assertTrue(proxy_mod.remove_user_proxy("1"))
                self.assertEqual(proxy_mod.user_proxies(), [])

    def test_auto_fallback_on_network_error(self):
        calls = []

        def fake_direct(req, timeout=None):
            calls.append(("direct", req.full_url))
            raise urllib.error.URLError("network down")

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"ok"

        def fake_proxy(req, timeout, proxy):
            calls.append(("proxy", proxy))
            return FakeResp()

        proxy_mod.set_mode("auto")
        req = urllib.request.Request("https://gemini.google.com/app")
        with patch("urllib.request.urlopen", side_effect=fake_direct), \
             patch.object(proxy_mod, "load_proxies", return_value=["http://p1:1", "http://p2:2"]), \
             patch.object(proxy_mod, "_open_with_proxy", side_effect=fake_proxy):
            resp = proxy_mod.urlopen(req, timeout=5)
            self.assertEqual(resp.read(), b"ok")
        self.assertEqual(calls[0][0], "direct")
        self.assertEqual(calls[1][0], "proxy")

    def test_no_fallback_on_http_error(self):
        def fake_direct(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"no"))

        proxy_mod.set_mode("auto")
        req = urllib.request.Request("https://gemini.google.com/app")
        with patch("urllib.request.urlopen", side_effect=fake_direct), \
             patch.object(proxy_mod, "load_proxies", return_value=["http://p1:1"]), \
             patch.object(proxy_mod, "_open_with_proxy") as p:
            with self.assertRaises(urllib.error.HTTPError):
                proxy_mod.urlopen(req, timeout=5)
            p.assert_not_called()

    def test_always_mode_skips_direct(self):
        calls = []

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_proxy(req, timeout, proxy):
            calls.append(proxy)
            return FakeResp()

        proxy_mod.set_mode("always")
        req = urllib.request.Request("https://gemini.google.com/app")
        with patch("urllib.request.urlopen", side_effect=AssertionError("direct called")), \
             patch.object(proxy_mod, "load_proxies", return_value=["http://p1:1"]), \
             patch.object(proxy_mod, "_open_with_proxy", side_effect=fake_proxy):
            proxy_mod.urlopen(req, timeout=5)
        self.assertEqual(calls, ["http://p1:1"])

    def test_http_error_from_destination_is_raised(self):
        def fake_proxy(req, timeout, proxy):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"no"))

        proxy_mod.set_mode("always")
        req = urllib.request.Request("https://gemini.google.com/app")
        with patch.object(proxy_mod, "load_proxies", return_value=["http://p1:1", "http://p2:2"]), \
             patch.object(proxy_mod, "_open_with_proxy", side_effect=fake_proxy) as p:
            with self.assertRaises(urllib.error.HTTPError):
                proxy_mod.urlopen(req, timeout=5)
            self.assertEqual(p.call_count, 1)


if __name__ == "__main__":
    unittest.main()
