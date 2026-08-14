#!/usr/bin/env python3
"""チャンネル文脈要約（summaries）のユニットテスト。"""

from unittest.mock import patch
import os
import tempfile
import unittest

from core import db
from core import summaries


class ThreadSummaryDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _add_message(self, conn, mid, ch, text, author=1, is_bot=False):
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name=f"ユーザー{author}", is_bot=is_bot)
        db.insert_message(conn, id=mid, channel_id=ch, author_id=author,
                          content=text, created_at="2026-07-16T12:00:00")

    def test_upsert_and_get(self):
        with db.connect(self.db_path) as conn:
            self.assertIsNone(db.get_thread_summary(conn, 1))
            db.upsert_thread_summary(conn, channel_id=1, summary="要約A",
                                     covered_until=10, updated_at="t1")
            db.upsert_thread_summary(conn, channel_id=1, summary="要約B",
                                     covered_until=20, updated_at="t2")
            row = db.get_thread_summary(conn, 1)
        self.assertEqual(row["summary"], "要約B")
        self.assertEqual(row["covered_until"], 20)

    def test_messages_after(self):
        with db.connect(self.db_path) as conn:
            for i in range(1, 6):
                self._add_message(conn, i, ch=7, text=f"msg{i}")
            self._add_message(conn, 99, ch=8, text="別ch")
            rows = db.messages_after(conn, 7, after_id=2)
        self.assertEqual([r["id"] for r in rows], [3, 4, 5])  # 古い順・別ch除外
        with db.connect(self.db_path) as conn:
            rows_all = db.messages_after(conn, 7, after_id=None)
        self.assertEqual(len(rows_all), 5)

    def test_latest_messages(self):
        with db.connect(self.db_path) as conn:
            for i in range(1, 11):
                self._add_message(conn, i, ch=11, text=f"m{i}")
            rows = db.latest_messages(conn, 11, limit=3)
        self.assertEqual([r["id"] for r in rows], [8, 9, 10])  # 最新3件を古い順で

    def test_messages_after_over_limit_consumes_oldest_first(self):
        # 新規が limit を超えても古い側から返し、続きから消化できること
        # （新しい側から取ると中間が恒久的に取りこぼされる）
        with db.connect(self.db_path) as conn:
            for i in range(1, 11):
                self._add_message(conn, i, ch=9, text=f"m{i}")
            rows = db.messages_after(conn, 9, after_id=None, limit=4)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4])
        with db.connect(self.db_path) as conn:
            rows2 = db.messages_after(conn, 9, after_id=4, limit=4)
        self.assertEqual([r["id"] for r in rows2], [5, 6, 7, 8])


class SummariesTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        with db.connect(self.db_path) as conn:
            db.upsert_user(conn, id=1, name="u1", display_name="管理者",
                           is_bot=False)
            for i in range(1, 11):
                db.insert_message(conn, id=i, channel_id=5, author_id=1,
                                  content=f"発言{i}",
                                  created_at="2026-07-16T12:00:00")

    def tearDown(self):
        os.unlink(self.db_path)

    def test_build_update_prompt(self):
        p = summaries.build_update_prompt("以前の要約",
                                          ["[2026-07-16] かば: こんにちは"])
        self.assertIn("以前の要約", p)
        self.assertIn("こんにちは", p)
        self.assertIn(str(summaries.SUMMARY_MAX_CHARS), p)

    def test_format_lines_truncates(self):
        rows = [{"author": "A", "content": "あ" * 1000,
                 "created_at": "2026-07-16T00:00:00", "is_bot": False}]
        lines = summaries.format_message_lines(rows)
        self.assertEqual(len(lines), 1)
        self.assertLess(len(lines[0]), summaries.PER_MESSAGE_CHARS + 50)

    def test_maybe_update_and_incremental_skip(self):
        calls = []

        def fake_invoke(prompt):
            calls.append(prompt)
            return "・新しい要約"

        out = summaries.maybe_update(self.db_path, 5, invoke_fn=fake_invoke)
        self.assertEqual(out, "・新しい要約")
        self.assertEqual(len(calls), 1)
        with db.connect(self.db_path) as conn:
            row = db.get_thread_summary(conn, 5)
        self.assertEqual(row["covered_until"], 10)
        # 差分がMIN_NEW_MESSAGES未満 → スキップ（CLI節約）
        out2 = summaries.maybe_update(self.db_path, 5, invoke_fn=fake_invoke)
        self.assertIsNone(out2)
        self.assertEqual(len(calls), 1)

    def test_maybe_update_skips_small_diff(self):
        out = summaries.maybe_update(
            self.db_path, 999,  # メッセージの無いch
            invoke_fn=lambda p: self.fail("呼ばれてはいけない"))
        self.assertIsNone(out)

    def test_get_summary_text_missing(self):
        self.assertEqual(summaries.get_summary_text(self.db_path, 5), "")

    def test_cold_start_summarizes_latest_not_oldest(self):
        # 初回（prevなし）は最古からでなく最新N件から要約を立ち上げる
        # （履歴の長いchで「現在の文脈」に追いつかない問題の防止）
        calls = []

        def fake_invoke(prompt):
            calls.append(prompt)
            return "・要約"

        with patch.object(summaries, "MIN_NEW_MESSAGES", 1), \
                patch.object(summaries, "MAX_MESSAGES_PER_UPDATE", 4):
            summaries.maybe_update(self.db_path, 5, invoke_fn=fake_invoke)
        self.assertIn("発言7", calls[0])
        self.assertIn("発言10", calls[0])
        self.assertNotIn("発言2", calls[0])  # 最古側は含まない
        with db.connect(self.db_path) as conn:
            row = db.get_thread_summary(conn, 5)
        self.assertEqual(row["covered_until"], 10)  # 最新まで一気にカバー

    def test_maybe_update_batches_without_loss(self):
        # 既存要約からの差分がlimit超でもバッチで段階消化され取りこぼしが無い
        calls = []

        def fake_invoke(prompt):
            calls.append(prompt)
            return "・要約"

        with db.connect(self.db_path) as conn:
            db.upsert_thread_summary(conn, channel_id=5, summary="既存",
                                     covered_until=2, updated_at="t0")
        with patch.object(summaries, "MIN_NEW_MESSAGES", 1), \
                patch.object(summaries, "MAX_MESSAGES_PER_UPDATE", 4):
            summaries.maybe_update(self.db_path, 5, invoke_fn=fake_invoke)
            with db.connect(self.db_path) as conn:
                first = db.get_thread_summary(conn, 5)["covered_until"]
            summaries.maybe_update(self.db_path, 5, invoke_fn=fake_invoke)
            with db.connect(self.db_path) as conn:
                second = db.get_thread_summary(conn, 5)["covered_until"]
        self.assertEqual(first, 6)    # 古い側（発言3〜6）から段階的に前進
        self.assertEqual(second, 10)  # 続き（発言7〜10）を消化
        self.assertIn("発言3", calls[0])
        self.assertIn("発言7", calls[1])
