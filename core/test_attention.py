#!/usr/bin/env python3
"""注意ループ（attention / 2026-08-23）のユニットテスト。

知覚（初回初期化・lull判定・checkpoint前進）・採点JSONの検証・候補管理
（1chに1懸念・due/expire）・再確認（解決なら取り下げ/未解決なら文面更新）・
投稿文面を検証する。claude は invoke_fn 注入で呼ばない。
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import attention

NOW = datetime(2026, 8, 23, 12, 0)   # JST正午
CH = 200


def utc_iso(y, mo, d, h, mi):
    """JST時刻をmessages.created_at形式（UTC ISO）に。"""
    from datetime import timedelta, timezone
    jst = datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=9)))
    return jst.astimezone(timezone.utc).isoformat()


class AttentionTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _msg(self, conn, mid, text, *, channel=CH, author=111,
             is_bot=False, at=(11, 0)):
        db.upsert_channel(conn, id=channel, name=f"ch{channel}", type="text")
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name=f"人{author}", is_bot=is_bot)
        db.insert_message(conn, id=mid, channel_id=channel, author_id=author,
                          content=text,
                          created_at=utc_iso(2026, 8, 23, *at))


class CollectTest(AttentionTestBase):
    def test_first_run_initializes_silently(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "過去の発言")
        self.assertIsNone(attention.collect_channel(
            self.db_path, "senko", CH, now=NOW))
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "新しい発言A", at=(11, 10))
            self._msg(conn, 3, "新しい発言B", at=(11, 20))
            self._msg(conn, 4, "新しい発言C", at=(11, 30))
        msgs = attention.collect_channel(self.db_path, "senko", CH, now=NOW)
        self.assertEqual([m["id"] for m in msgs], [2, 3, 4])
        # checkpoint前進済み → 次回は新着なし
        self.assertIsNone(attention.collect_channel(
            self.db_path, "senko", CH, now=NOW))

    def test_hot_conversation_deferred(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "seed")
        attention.collect_channel(self.db_path, "senko", CH, now=NOW)
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "会話中A", at=(11, 50))
            self._msg(conn, 3, "会話中B", at=(11, 55))
            self._msg(conn, 4, "会話中C", at=(11, 58))  # 2分前=まだ熱い
        self.assertIsNone(attention.collect_channel(
            self.db_path, "senko", CH, now=NOW))
        # lullが来たら同じ差分が取れる（checkpointは動いていない）
        later = datetime(2026, 8, 23, 12, 30)
        msgs = attention.collect_channel(self.db_path, "senko", CH, now=later)
        self.assertEqual([m["id"] for m in msgs], [2, 3, 4])

    def test_tiny_diff_consumed_silently(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "seed")
        attention.collect_channel(self.db_path, "senko", CH, now=NOW)
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "相槌", at=(11, 0))
        self.assertIsNone(attention.collect_channel(
            self.db_path, "senko", CH, now=NOW))
        # 消化済み（次周期にも出てこない）
        with db.connect(self.db_path) as conn:
            self._msg(conn, 3, "追加A", at=(11, 10))
            self._msg(conn, 4, "追加B", at=(11, 15))
            self._msg(conn, 5, "追加C", at=(11, 20))
        msgs = attention.collect_channel(self.db_path, "senko", CH, now=NOW)
        self.assertEqual([m["id"] for m in msgs], [3, 4, 5])

    def test_reactions_included(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "seed")
        attention.collect_channel(self.db_path, "senko", CH, now=NOW)
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "質問です", at=(11, 0))
            self._msg(conn, 3, "補足", at=(11, 1))
            self._msg(conn, 4, "さらに補足", at=(11, 2))
            db.upsert_user(conn, id=333, name="u333",
                           display_name="瓜生", is_bot=False)
            db.add_reaction(conn, message_id=2, emoji="👍", user_id=333,
                            created_at="2026-08-23T11:05")
        msgs = attention.collect_channel(self.db_path, "senko", CH, now=NOW)
        self.assertEqual(msgs[0]["reactions"], [("👍", "瓜生")])


class ScoreParseTest(unittest.TestCase):
    def test_valid(self):
        out = attention.parse_score_response(
            '{"score": 7, "after_message_id": 3, "mode": "問いかけ", '
            '"say": "確認しましょうか?", "reason": "宙ぶらりん"}', {1, 2, 3})
        self.assertEqual((out["score"], out["anchor_message_id"]), (7, 3))

    def test_unknown_anchor_falls_back_to_last(self):
        out = attention.parse_score_response(
            '{"score": 6, "after_message_id": 999, "mode": "一言", '
            '"say": "どうします?", "reason": "r"}', {1, 2, 3})
        self.assertEqual(out["anchor_message_id"], 3)

    def test_broken_or_out_of_range(self):
        self.assertIsNone(attention.parse_score_response("無理", {1}))
        self.assertIsNone(attention.parse_score_response(
            '{"score": 11, "say": "x"}', {1}))
        self.assertIsNone(attention.parse_score_response(
            '{"score": 5, "say": ""}', {1}))   # 発言案なしは静観

    def test_prompt_mentions_reactions(self):
        msgs = [{"id": 1, "author": "A", "content": "質問",
                 "created_at": utc_iso(2026, 8, 23, 11, 0),
                 "reactions": [("👍", "瓜生")]}]
        p = attention.build_score_prompt(msgs, "AI戦子")
        self.assertIn("👍(瓜生)", p)
        self.assertIn("AI戦子", p)


class CandidateFlowTest(AttentionTestBase):
    JUDGED = {"score": 7, "anchor_message_id": 3, "mode": "問いかけ",
              "say": "どうします?", "reason": "宙ぶらりん"}

    def test_save_and_single_pending_per_channel(self):
        attention.save_candidate(self.db_path, "senko", CH, self.JUDGED,
                                 grace_hours=4, now=NOW)
        with db.connect(self.db_path) as conn:
            self.assertTrue(db.pending_attention_exists(conn, "senko", CH))
            self.assertFalse(db.pending_attention_exists(conn, "senko", 999))
            items = db.pending_attention_items(conn, "senko")
        self.assertEqual(items[0]["due_at"], "2026-08-23T16:00")

    def test_speak_action_wait_speak_expire(self):
        item = {"due_at": "2026-08-23T16:00"}
        self.assertEqual(attention.speak_action(
            item, datetime(2026, 8, 23, 15, 0)), "wait")
        self.assertEqual(attention.speak_action(
            item, datetime(2026, 8, 23, 17, 0)), "speak")
        self.assertEqual(attention.speak_action(
            item, datetime(2026, 8, 26, 17, 0)), "expire")

    def test_status_terminal(self):
        attention.save_candidate(self.db_path, "senko", CH, self.JUDGED,
                                 now=NOW)
        with db.connect(self.db_path) as conn:
            items = db.pending_attention_items(conn, "senko")
            db.set_attention_status(conn, items[0]["id"], "resolved")
            self.assertEqual(db.pending_attention_items(conn, "senko"), [])


class RecheckTest(AttentionTestBase):
    ITEM = {"id": 1, "channel_id": CH, "anchor_message_id": 2,
            "say": "どうします?", "reason": "担当が決まってない"}

    def test_resolved_by_later_talk(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "誰かやっといて", at=(10, 0))
            self._msg(conn, 3, "俺がやります", at=(10, 30))
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"resolved": true}'
        resolved, _say = attention.recheck(
            self.db_path, self.ITEM, agent_name="AI戦子", invoke_fn=fake)
        self.assertTrue(resolved)
        self.assertIn("俺がやります", seen["prompt"])
        self.assertIn("担当が決まってない", seen["prompt"])

    def test_unresolved_refreshes_say(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "誰かやっといて", at=(10, 0))
            self._msg(conn, 3, "うーん", at=(10, 30))
        resolved, say = attention.recheck(
            self.db_path, self.ITEM, agent_name="AI戦子",
            invoke_fn=lambda p: '{"resolved": false, "say": "更新後の文面"}')
        self.assertEqual((resolved, say), (False, "更新後の文面"))

    def test_error_falls_back_to_original_say(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "誰かやっといて", at=(10, 0))

        def broken(_p):
            raise RuntimeError("claude down")
        resolved, say = attention.recheck(
            self.db_path, self.ITEM, agent_name="AI戦子", invoke_fn=broken)
        self.assertEqual((resolved, say), (False, "どうします?"))

    def test_build_message_has_soft_landing(self):
        text = attention.build_message("どうします?")
        self.assertIn("どうします?", text)
        self.assertIn("スルーで大丈夫", text)


if __name__ == "__main__":
    unittest.main()
