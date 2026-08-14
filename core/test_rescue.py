#!/usr/bin/env python3
"""未回答質問の救済（rescue / 進化ロードマップ#31）のユニットテスト。

候補抽出（時間窓・リプライ有無・重複判定・除外ch）・判定パース・
プロンプト内容・記録の重複防止を検証する。claudeは invoke_fn 注入。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core import db
from core import rescue

NOW_UTC = datetime(2026, 7, 31, 12, 0)


class RescueTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _msg(self, conn, mid, text, *, hours_ago=30, ch=1, author=1,
             is_bot=False, reply_to=None):
        db.upsert_channel(conn, id=ch, name="general", type="text")
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name=f"ユーザー{author}", is_bot=is_bot)
        created = (NOW_UTC - timedelta(hours=hours_ago)).isoformat() \
            + "+00:00"
        db.insert_message(conn, id=mid, channel_id=ch, author_id=author,
                          content=text, created_at=created,
                          reply_to=reply_to)

    def _find(self, **kw):
        return rescue.find_candidates(self.db_path, now=NOW_UTC, **kw)


class CandidateTest(RescueTestBase):
    def test_aged_unanswered_question_is_found(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "スカジャンの納期っていつでしたっけ？",
                      hours_ago=30)
        cands = self._find()
        self.assertEqual([c["id"] for c in cands], [10])

    def test_window_bounds(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "新しい質問です？", hours_ago=2)    # 若すぎ
            self._msg(conn, 11, "古すぎる質問？", hours_ago=100)   # 古すぎ
            self._msg(conn, 12, "ちょうどいい質問？", hours_ago=30)
        self.assertEqual([c["id"] for c in self._find()], [12])

    def test_replied_question_is_excluded(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "これどうですか？", hours_ago=30)
            self._msg(conn, 11, "こうだよ", hours_ago=29, author=2,
                      reply_to=10)
        self.assertEqual(self._find(), [])

    def test_bot_and_nonquestion_and_noise_excluded(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "Botの質問？", hours_ago=30, author=9,
                      is_bot=True)
            self._msg(conn, 11, "質問じゃない発言", hours_ago=30)
            self._msg(conn, 12, "まじ？", hours_ago=30)   # 相槌ノイズ
        self.assertEqual(self._find(), [])

    def test_excluded_channels_and_dedupe(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "除外chの質問？", hours_ago=30, ch=200)
            self._msg(conn, 11, "判定済みの質問？", hours_ago=30)
        rescue.record(self.db_path, 11, "agent1", "skipped_answered")
        self.assertEqual(self._find(exclude_channel_ids=[200]), [])


class JudgeTest(RescueTestBase):
    CAND = {"id": 10, "channel_id": 1, "channel": "general",
            "author": "かば", "author_id": 1,
            "content": "スカジャンの納期っていつでしたっけ？",
            "created_at": "2026-07-30T06:00:00+00:00"}

    def test_prompt_contains_question_and_flow(self):
        follow = [{"id": 11, "author": "山田", "is_bot": False,
                   "content": "別の話だけど…", "created_at": "t"}]
        prompt = rescue.build_judge_prompt(self.CAND, follow)
        self.assertIn("納期っていつ", prompt)
        self.assertIn("山田: 別の話だけど", prompt)
        self.assertIn("迷ったら救済しない", prompt)

    def test_parse_judge_safe_side(self):
        self.assertTrue(rescue.parse_judge(
            '{"rescue": true, "reason": "未回答"}')["rescue"])
        self.assertFalse(rescue.parse_judge(
            '{"rescue": false, "reason": "解決済み"}')["rescue"])
        self.assertFalse(rescue.parse_judge("判定できません")["rescue"])
        self.assertFalse(rescue.parse_judge("")["rescue"])

    def test_judge_uses_invoke_fn(self):
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"rescue": true, "reason": "誰も答えていない"}'

        v = rescue.judge(self.CAND, [], model="x", invoke_fn=fake)
        self.assertTrue(v["rescue"])
        self.assertIn("納期", seen["prompt"])


class RecordTest(RescueTestBase):
    def test_record_prevents_rejudge(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "質問です？", hours_ago=30)
        self.assertEqual(len(self._find()), 1)
        rescue.record(self.db_path, 10, "agent1", "shadow")
        self.assertEqual(self._find(), [])


if __name__ == "__main__":
    unittest.main()
