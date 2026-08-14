#!/usr/bin/env python3
"""録音セッションの劣化要因対策のユニットテスト（discord接続不要）。

実行: ./venv/bin/python -m unittest test_meeting_recording -v

対象:
- 増分PCM追記→finalize（問題1: イベントループ停止と長会議スパイクの解消）
- stale_recording_dirs（問題5: 14日prune・未処理dir保護）
- should_end_meeting（問題3: 退出取りこぼしの保険）
"""

import json
import os
import tempfile
import unittest
import wave
from collections import deque
from types import SimpleNamespace

# bot.py は import 時にトークン必須。テストではダミーで通す（bot.runはしない）
os.environ.setdefault("DISCORD_MEETINGBOT_TOKEN", "test-dummy-token")

# 議事録BOTは音声の重い依存（discord-ext-voice-recv / faster-whisper）を使う。
# 既定OFFの機能なので、依存が入っていない環境ではテストごとスキップする
# （requirements.txt を入れれば普通に走る）。
try:
    from platforms.discord.meeting import bot
except ImportError as _e:  # pragma: no cover - 依存未導入の環境用
    bot = None
    _SKIP_REASON = (f"議事録BOTの依存が未導入です（{_e}）。"
                    "meetingbot/requirements.txt を入れると実行されます")
else:
    _SKIP_REASON = ""

pytestmark = unittest.skipIf(bot is None, _SKIP_REASON)


def load_tests(loader, tests, pattern):
    """依存が無い環境ではこのモジュールのテストを丸ごとスキップする。"""
    if bot is None:
        suite = unittest.TestSuite()

        class DependencyMissing(unittest.TestCase):
            @unittest.skip(_SKIP_REASON)
            def test_skipped(self):
                pass

        suite.addTest(DependencyMissing("test_skipped"))
        return suite
    return tests


class IncrementalAudioTest(unittest.TestCase):
    """チェックポイントは増分追記のみ（O(新規分)）で、stop時のfinalizeで
    transcriberが読める .wav / _timestamps.json に確定することを検証。"""

    def test_append_then_finalize_produces_wav_and_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            base = "アーカイブ担当_123"
            # 2回に分けて追記（＝2回のチェックポイントを模す）
            bot.append_pcm_chunk(d, base, b"\x01\x02" * 480, [1000.0, 1000.02])
            bot.append_pcm_chunk(d, base, b"\x03\x04" * 480, [1000.04])
            # 追記段階では中間ファイルのみ、まだ .wav は無い
            self.assertTrue(os.path.exists(f"{d}/{base}.pcm"))
            self.assertFalse(os.path.exists(f"{d}/{base}.wav"))

            bot.finalize_recording_files(d, base)

            wav_path = f"{d}/{base}.wav"
            ts_path = f"{d}/{base}_timestamps.json"
            self.assertTrue(os.path.exists(wav_path))
            self.assertTrue(os.path.exists(ts_path))
            # 中間ファイルは掃除される
            self.assertFalse(os.path.exists(f"{d}/{base}.pcm"))
            self.assertFalse(os.path.exists(f"{d}/{base}_timestamps.jsonl"))
            # WAVは全PCMを連結した長さ、フォーマットはbotの定数どおり
            with wave.open(wav_path, "rb") as wf:
                self.assertEqual(wf.getnchannels(), bot.CHANNELS)
                self.assertEqual(wf.getsampwidth(), bot.SAMPLE_WIDTH)
                self.assertEqual(wf.getframerate(), bot.SAMPLE_RATE)
                frames = wf.readframes(wf.getnframes())
            # 2回の追記（各 480要素×2バイト = 960バイト）を連結した長さ
            self.assertEqual(len(frames), 960 * 2)
            # timestampsは追記順のまま配列化（transcriberの_load_timestamps互換）
            with open(ts_path) as f:
                self.assertEqual(json.load(f), [1000.0, 1000.02, 1000.04])

    def test_finalize_skips_user_without_pcm(self):
        # PCMが一度も無いユーザーは .wav を作らない（無音話者でクラッシュしない）
        with tempfile.TemporaryDirectory() as d:
            bot.finalize_recording_files(d, "無音_999")
            self.assertFalse(os.path.exists(f"{d}/無音_999.wav"))


class SinkDrainTest(unittest.TestCase):
    """flushでメモリバッファ（deque）が書き出し済み分だけ解放されること、
    opus由来データを溜め込まずカウンタだけ持つことを検証（問題2）。"""

    def _make_sink(self):
        try:
            return bot.MeetingSink(vc=None)
        except Exception as e:  # AudioSink基底が生成不能な環境ならスキップ
            self.skipTest(f"MeetingSink生成不可: {e}")

    def test_flush_drains_buffer(self):
        sink = self._make_sink()
        user = SimpleNamespace(id=7, name="かば")
        sink.audio_buffers[7] = {
            "user": user, "chunk_count": 2,
            "pcm_chunks": deque([b"\xaa\xbb" * 480, b"\xcc\xdd" * 480]),
            "pcm_timestamps": deque([500.0, 500.02]),
        }
        with tempfile.TemporaryDirectory() as d:
            sink.flush_to_disk(d)
            # バッファは解放され、ディスクには追記済み
            self.assertEqual(len(sink.audio_buffers[7]["pcm_chunks"]), 0)
            self.assertEqual(len(sink.audio_buffers[7]["pcm_timestamps"]), 0)
            self.assertTrue(os.path.exists(f"{d}/かば_7.pcm"))
            # 追記後にさらに書いてfinalize → 全チャンクが残っている
            sink.audio_buffers[7]["pcm_chunks"].append(b"\xee\xff" * 480)
            sink.audio_buffers[7]["pcm_timestamps"].append(500.04)
            sink.finalize(d)
            with wave.open(f"{d}/かば_7.wav", "rb") as wf:
                self.assertEqual(len(wf.readframes(wf.getnframes())),
                                 480 * 2 * 3)

    def test_buffer_has_no_opus_chunks(self):
        # 中身未使用のopus_chunksは廃止しカウンタ化（メモリ肥大の主因を除去）
        sink = self._make_sink()
        user = SimpleNamespace(id=1, name="x")
        sink._ensure_user(user)
        self.assertNotIn("opus_chunks", sink.audio_buffers[1])
        self.assertEqual(sink.audio_buffers[1]["chunk_count"], 0)


class StaleDirsTest(unittest.TestCase):
    """14日prune。未処理（pending/failed残存）は保護（問題5）。"""

    DAY = 86400

    def test_old_dir_is_stale(self):
        now = 1_000_000_000.0
        entries = [("recordings/old", now - 15 * self.DAY, False)]
        self.assertEqual(bot.stale_recording_dirs(entries, now, 14),
                         ["recordings/old"])

    def test_recent_dir_kept(self):
        now = 1_000_000_000.0
        entries = [("recordings/new", now - 3 * self.DAY, False)]
        self.assertEqual(bot.stale_recording_dirs(entries, now, 14), [])

    def test_unprocessed_dir_protected_even_if_old(self):
        now = 1_000_000_000.0
        entries = [("recordings/stuck", now - 30 * self.DAY, True)]
        self.assertEqual(bot.stale_recording_dirs(entries, now, 14), [])

    def test_boundary_exactly_14_days_kept(self):
        now = 1_000_000_000.0
        entries = [("recordings/edge", now - 14 * self.DAY, False)]
        # ちょうど14日は保持、超えたら削除（>）
        self.assertEqual(bot.stale_recording_dirs(entries, now, 14), [])


class ShouldEndMeetingTest(unittest.TestCase):
    """接続復帰後に対象VCが連続して無人なら終了（退出取りこぼしの保険）。"""

    def test_stays_while_people_present(self):
        polls, end = bot.should_end_meeting(non_bot_count=1, empty_polls=1)
        self.assertEqual(polls, 0)
        self.assertFalse(end)

    def test_ends_after_consecutive_empty(self):
        polls, end = bot.should_end_meeting(non_bot_count=0, empty_polls=0)
        self.assertEqual(polls, 1)
        self.assertFalse(end)
        polls, end = bot.should_end_meeting(non_bot_count=0, empty_polls=polls)
        self.assertEqual(polls, 2)
        self.assertTrue(end)


if __name__ == "__main__":
    unittest.main()
