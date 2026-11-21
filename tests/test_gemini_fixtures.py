"""Разбор сохранённых кадров Gemini (фикстуры, без сети)."""

from __future__ import annotations

import unittest
from pathlib import Path

from aifree.gemini import clean_text, is_failure_reply, parse_bl, parse_frame_texts

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"


def lines(name: str) -> list[str]:
    return FIXTURES.joinpath(name).read_text(encoding="utf-8").splitlines()


class GeminiFixtureTests(unittest.TestCase):
    def test_normal_frame(self):
        texts = parse_frame_texts(lines("frame_normal.jsonl")[0])
        self.assertEqual(texts, ["Hello from fixture"])

    def test_stream_frames_are_cumulative(self):
        parsed = [parse_frame_texts(line) for line in lines("frame_stream.jsonl")]
        self.assertEqual(parsed[0], ["Hel"])
        self.assertEqual(parsed[1], ["Hello from fixture"])

    def test_followup_tag_is_removed(self):
        texts = parse_frame_texts(lines("frame_followup.jsonl")[0])
        cleaned = clean_text(texts[-1])
        self.assertEqual(cleaned, "Готово")
        self.assertNotIn("FollowUp", cleaned)

    def test_failure_phrase(self):
        texts = parse_frame_texts(lines("frame_failure.jsonl")[0])
        self.assertTrue(is_failure_reply(texts[-1]))

    def test_empty_frame(self):
        self.assertEqual(parse_frame_texts(lines("frame_empty.jsonl")[0]), [])

    def test_page_build_id(self):
        page = FIXTURES.joinpath("page.html").read_text(encoding="utf-8")
        self.assertEqual(
            parse_bl(page), "boq_assistant-bard-web-server_20260101.00_p0"
        )
        self.assertIsNone(parse_bl("<html>no build id</html>"))


if __name__ == "__main__":
    unittest.main()
