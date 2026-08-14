#!/usr/bin/env python3
"""二重反応対策4点セット（2026-08-07）のユニットテスト。

① 宛先つき発言の観察除外 ② トリガー横断クレーム（CAS）
③ handoff権限の限定（config・agent_loops側） ④ 対象者の既回答チェック
"""

import os
import tempfile
import unittest

from core import db
from core import proactive

AGENT_UIDS = (111, 222, 333)


class FilterAddressedTest(unittest.TestCase):
    def _m(self, mid, content, reply_to=None):
        return {"id": mid, "content": content, "reply_to": reply_to}

    def test_mention_to_agent_excluded(self):
        msgs = [self._m(1, "<@222> SNSの投稿どうなってる？"),
                self._m(2, "今日は暑いね")]
        kept, dropped = proactive.filter_addressed(msgs, AGENT_UIDS, {})
        self.assertEqual([m["id"] for m in kept], [2])
        self.assertEqual([m["id"] for m in dropped], [1])

    def test_nickname_mention_form_excluded(self):
        msgs = [self._m(1, "<@!333> お願い")]
        kept, _ = proactive.filter_addressed(msgs, AGENT_UIDS, {})
        self.assertEqual(kept, [])

    def test_reply_to_agent_excluded(self):
        msgs = [self._m(5, "それでお願いします", reply_to=99)]
        kept, dropped = proactive.filter_addressed(
            msgs, AGENT_UIDS, {99: 222})     # 99はエージェントの発言
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_reply_to_human_kept(self):
        msgs = [self._m(5, "それでお願いします", reply_to=99)]
        kept, _ = proactive.filter_addressed(msgs, AGENT_UIDS, {99: 777})
        self.assertEqual(len(kept), 1)

    def test_human_mention_kept(self):
        msgs = [self._m(1, "<@999> どう思う？")]   # 人間宛メンション
        kept, _ = proactive.filter_addressed(msgs, AGENT_UIDS, {})
        self.assertEqual(len(kept), 1)


class DbGateTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_claim_is_first_wins(self):
        with db.connect(self.db_path) as conn:
            self.assertTrue(db.claim_trigger(conn, 500, "agent1",
                                             "recall", "t"))
            # 別エージェントの同一トリガーは負ける
            self.assertFalse(db.claim_trigger(conn, 500, "agent2",
                                              "handoff", "t"))
            # 別トリガーは通る
            self.assertTrue(db.claim_trigger(conn, 501, "agent2",
                                             "handoff", "t"))

    def test_agent_posted_after(self):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=7, name="sns", type="text")
            db.upsert_user(conn, id=333, name="rufu", display_name="エージェント3",
                           is_bot=True)
            db.insert_message(conn, id=100, channel_id=7, author_id=1,
                              content="質問", created_at="t")
            self.assertFalse(db.agent_posted_after(conn, 7, 333, 100))
            db.insert_message(conn, id=101, channel_id=7, author_id=333,
                              content="回答", created_at="t")
            self.assertTrue(db.agent_posted_after(conn, 7, 333, 100))
            # 別chの発言は数えない
            self.assertFalse(db.agent_posted_after(conn, 8, 333, 100))

    def test_collect_cycle_excludes_addressed(self):
        """統合: エージェント宛メンションつき発言はdigestに載らない。"""
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=7, name="g", type="text")
            db.upsert_user(conn, id=1, name="h", display_name="山田",
                           is_bot=False)
        # 初回=初期化
        self.assertIsNone(proactive.collect_cycle(
            self.db_path, "agent1", home_channel_id=999,
            agent_user_ids=AGENT_UIDS))
        with db.connect(self.db_path) as conn:
            db.insert_message(conn, id=10, channel_id=7, author_id=1,
                              content="<@222> SNS更新して",
                              created_at="2026-08-07 10:00")
            db.insert_message(conn, id=11, channel_id=7, author_id=1,
                              content="通常の発言",
                              created_at="2026-08-07 10:01")
        digest = proactive.collect_cycle(
            self.db_path, "agent1", home_channel_id=999,
            agent_user_ids=AGENT_UIDS)
        self.assertEqual([m["id"] for m in digest["messages"]], [11])

    def test_collect_cycle_all_addressed_returns_none(self):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=7, name="g", type="text")
            db.upsert_user(conn, id=1, name="h", display_name="山田",
                           is_bot=False)
        self.assertIsNone(proactive.collect_cycle(
            self.db_path, "agent1", home_channel_id=999,
            agent_user_ids=AGENT_UIDS))
        with db.connect(self.db_path) as conn:
            db.insert_message(conn, id=10, channel_id=7, author_id=1,
                              content="<@222> お願い",
                              created_at="2026-08-07 10:00")
        self.assertIsNone(proactive.collect_cycle(
            self.db_path, "agent1", home_channel_id=999,
            agent_user_ids=AGENT_UIDS))


if __name__ == "__main__":
    unittest.main()


class HandoffTargetTest(unittest.TestCase):
    """引き継ぎ先の設定フィルタ（2026-08-12: デザイン担当への引き継ぎオフ）。"""

    COLLEAGUES = {"agent2": ("エージェント2", "デザイン"),
                  "agent3": ("エージェント3", "SNS")}

    def test_true_allows_all(self):
        self.assertEqual(
            proactive.allowed_handoff_targets(True, self.COLLEAGUES),
            self.COLLEAGUES)

    def test_list_restricts(self):
        out = proactive.allowed_handoff_targets(["agent3"],
                                                self.COLLEAGUES)
        self.assertEqual(list(out), ["agent3"])

    def test_false_or_none_disables(self):
        self.assertEqual(
            proactive.allowed_handoff_targets(False, self.COLLEAGUES), {})
        self.assertEqual(
            proactive.allowed_handoff_targets(None, self.COLLEAGUES), {})

    def test_unknown_id_ignored(self):
        self.assertEqual(
            proactive.allowed_handoff_targets(["ghost"], self.COLLEAGUES),
            {})
