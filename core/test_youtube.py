#!/usr/bin/env python3
"""YouTube要約（youtube_summary）のユニットテスト。"""

import unittest

from core import youtube_summary


class ExtractVideoIdsTest(unittest.TestCase):
    VID = "dQw4w9WgXcQ"

    def one(self, text):
        found = youtube_summary.extract_video_ids(text)
        self.assertEqual(len(found), 1, text)
        return found[0]

    def test_watch_url(self):
        vid, url = self.one(f"https://www.youtube.com/watch?v={self.VID}")
        self.assertEqual(vid, self.VID)
        self.assertEqual(url, f"https://www.youtube.com/watch?v={self.VID}")

    def test_youtu_be(self):
        vid, _ = self.one(f"これ見て https://youtu.be/{self.VID} すごいよ")
        self.assertEqual(vid, self.VID)

    def test_shorts_and_live(self):
        for path in ("shorts", "live"):
            vid, _ = self.one(f"https://youtube.com/{path}/{self.VID}")
            self.assertEqual(vid, self.VID, path)

    def test_angle_wrapped_url(self):
        vid, _ = self.one(f"<https://youtu.be/{self.VID}>")
        self.assertEqual(vid, self.VID)

    def test_url_with_params(self):
        vid, _ = self.one(
            f"https://www.youtube.com/watch?v={self.VID}&t=42s")
        self.assertEqual(vid, self.VID)
        vid, _ = self.one(
            f"https://www.youtube.com/watch?list=PLx&v={self.VID}")
        self.assertEqual(vid, self.VID)

    def test_playlist_and_bare_id_not_matched(self):
        for text in ("https://www.youtube.com/playlist?list=PLxxxxxxxxxxx",
                     "https://www.youtube.com/@somechannel",
                     self.VID,  # 裸の11桁ID
                     "動画IDっぽい dQw4w9WgXcQ という文字列"):
            self.assertEqual(
                youtube_summary.extract_video_ids(text), [], text)

    def test_multiple_urls_dedup(self):
        found = youtube_summary.extract_video_ids(
            f"https://youtu.be/{self.VID} と "
            f"https://youtu.be/abcdefghijk と "
            f"https://www.youtube.com/watch?v={self.VID}")
        self.assertEqual([v for v, _ in found], [self.VID, "abcdefghijk"])

    def test_empty_and_none(self):
        self.assertEqual(youtube_summary.extract_video_ids(""), [])
        self.assertEqual(youtube_summary.extract_video_ids(None), [])


class YoutubeSummaryLogicTest(unittest.TestCase):
    def test_strip_urls(self):
        self.assertEqual(
            youtube_summary.strip_urls(
                "この動画要約して <https://youtu.be/dQw4w9WgXcQ> お願い"),
            "この動画要約して  お願い")

    def test_strip_urls_url_only(self):
        self.assertEqual(
            youtube_summary.strip_urls("https://youtu.be/dQw4w9WgXcQ"), "")

    def test_trim_short_text_unchanged(self):
        text = "あ" * youtube_summary.MAX_TRANSCRIPT_CHARS
        self.assertEqual(youtube_summary.trim_transcript(text), text)

    def test_trim_long_text_keeps_head_and_tail(self):
        text = ("H" * youtube_summary.TRIM_HEAD_CHARS
                + "M" * 10000
                + "T" * youtube_summary.TRIM_TAIL_CHARS)
        trimmed = youtube_summary.trim_transcript(text)
        self.assertLess(len(trimmed), len(text))
        self.assertTrue(trimmed.startswith("H"))
        self.assertTrue(trimmed.endswith("T"))
        self.assertIn("（中略）", trimmed)
        self.assertNotIn("M", trimmed)

    def test_build_prompt_contains_parts(self):
        p = youtube_summary.build_prompt(
            "ペルソナ文。\n\n", "サンプル動画", 12, "要点だけ教えて", "字幕本文")
        self.assertTrue(p.startswith("ペルソナ文。"))
        for part in ("サンプル動画", "約12分", "要点だけ教えて", "字幕本文",
                     "創作しない"):
            self.assertIn(part, p)

    def test_build_prompt_defaults(self):
        p = youtube_summary.build_prompt("", None, 5, "", "字幕")
        self.assertIn("【動画タイトル】不明", p)
        self.assertIn("（URLのみ投稿）", p)
