#!/usr/bin/env python3
"""
transcriber の純粋ロジックのユニットテスト（whisperモデル不要）。
実行: ./venv/bin/python -m unittest test_transcriber -v
"""

import unittest

from platforms.discord.meeting.transcriber import build_transcript


class BuildTranscriptTest(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        # 発話ゼロ（無音・テスト入室）でクラッシュせず空文字（回帰テスト）
        self.assertEqual(build_transcript([], has_real_timestamps=True), "")
        self.assertEqual(build_transcript([], has_real_timestamps=False), "")

    def test_relative_time_fallback(self):
        segs = [(5.0, "アーカイブ担当", "こんにちは"), (65.0, "かば", "どうも")]
        out = build_transcript(segs, has_real_timestamps=False)
        self.assertEqual(
            out, "[00:05] アーカイブ担当: こんにちは\n[01:05] かば: どうも")

    def test_real_time_uses_first_as_base(self):
        # 実時間: 最初の発話を 00:00 として経過時間を出す
        segs = [(1000.0, "A", "start"), (1075.0, "B", "later")]
        out = build_transcript(segs, has_real_timestamps=True)
        self.assertEqual(out, "[00:00] A: start\n[01:15] B: later")

    def test_sorts_by_time(self):
        segs = [(30.0, "B", "second"), (10.0, "A", "first")]
        out = build_transcript(segs, has_real_timestamps=False)
        self.assertTrue(out.startswith("[00:10] A: first"))


if __name__ == "__main__":
    unittest.main()
