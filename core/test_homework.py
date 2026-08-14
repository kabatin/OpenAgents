#!/usr/bin/env python3
"""宿題検出（homework / エージェントv3 Phase E）のユニットテスト。

差分収集（初回初期化・checkpoint前進）・検知JSONの検証（自己コミットのみ・
迷ったら捨てる）・期日の算出・声かけ判定（wait/ask/expire）・保存（owner=発言者・
冪等）・声かけ文面を検証する。claude は invoke_fn 注入で呼ばない。
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import homework

NOW = datetime(2026, 7, 31, 12, 0)
HOME_CH = 100
CH = 200


class HomeworkTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _msg(self, conn, mid, text, *, channel=CH, author=111, is_bot=False):
        db.upsert_channel(conn, id=channel, name=f"ch{channel}", type="text")
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name=f"人{author}", is_bot=is_bot)
        db.insert_message(conn, id=mid, channel_id=channel, author_id=author,
                          content=text, created_at="2026-07-31T03:00:00+00:00")


class CollectTest(HomeworkTestBase):
    def test_first_run_initializes_silently(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "あとで見積もり確認しとく")
        # 初回は「今」に初期化するだけで過去は拾わない
        self.assertIsNone(homework.collect_new_messages(
            self.db_path, "agent1", home_channel_id=HOME_CH, now=NOW))
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "あとで請求書やっとくわ")
            self._msg(conn, 3, "botの発言", author=900, is_bot=True)
            self._msg(conn, 4, "homeでの雑談", channel=HOME_CH)
        digest = homework.collect_new_messages(
            self.db_path, "agent1", home_channel_id=HOME_CH, now=NOW)
        ids = [m["id"] for m in digest["messages"]]
        self.assertEqual(ids, [2])  # Bot・homeチャンネルは除外
        # checkpoint前進済み → 次回は新着なし
        self.assertIsNone(homework.collect_new_messages(
            self.db_path, "agent1", home_channel_id=HOME_CH, now=NOW))

    def test_exclude_channel_ids(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "seed")
        homework.collect_new_messages(
            self.db_path, "agent1", home_channel_id=HOME_CH, now=NOW)
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "除外chの発言", channel=300)
            self._msg(conn, 3, "通常chの発言", channel=CH)
        digest = homework.collect_new_messages(
            self.db_path, "agent1", home_channel_id=HOME_CH,
            exclude_channel_ids=[300], now=NOW)
        self.assertEqual([m["id"] for m in digest["messages"]], [3])


class ParseDetectTest(unittest.TestCase):
    VALID = {1, 2, 3}

    def test_valid_commitment(self):
        raw = ('前置き {"commitments": [{"message_id": 2, '
               '"task": "見積もりの確認"}]} 後置き')
        out = homework.parse_detect_response(raw, self.VALID)
        self.assertEqual(out, [{"message_id": 2, "task": "見積もりの確認"}])

    def test_empty_and_broken(self):
        self.assertEqual(
            homework.parse_detect_response('{"commitments": []}', self.VALID),
            [])
        self.assertEqual(
            homework.parse_detect_response("該当なし", self.VALID), [])

    def test_unknown_id_and_missing_task_dropped(self):
        raw = ('{"commitments": [{"message_id": 99, "task": "圏外"}, '
               '{"message_id": 1, "task": ""}, '
               '{"message_id": 3, "task": "調べる"}]}')
        out = homework.parse_detect_response(raw, self.VALID)
        self.assertEqual(out, [{"message_id": 3, "task": "調べる"}])

    def test_duplicate_id_dropped(self):
        raw = ('{"commitments": [{"message_id": 1, "task": "a"}, '
               '{"message_id": 1, "task": "b"}]}')
        out = homework.parse_detect_response(raw, self.VALID)
        self.assertEqual(out, [{"message_id": 1, "task": "a"}])

    def test_candidate_cap(self):
        cands = ",".join(
            f'{{"message_id": {i}, "task": "t{i}"}}' for i in range(1, 4))
        raw = '{"commitments": [' + cands + ']}'
        out = homework.parse_detect_response(raw, self.VALID)
        self.assertLessEqual(len(out), homework.MAX_CANDIDATES)

    def test_detect_uses_invoke_fn(self):
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"commitments": [{"message_id": 5, "task": "x"}]}'

        msgs = [{"id": 5, "channel": "ch", "author": "太郎",
                 "content": "あとでやる"}]
        out = homework.detect(msgs, agent_name="アーカイブ担当", invoke_fn=fake)
        self.assertEqual(out, [{"message_id": 5, "task": "x"}])
        self.assertIn("あとでやる", seen["prompt"])
        self.assertIn("アーカイブ担当", seen["prompt"])


class FollowUpDateTest(unittest.TestCase):
    def test_follow_up_date(self):
        self.assertEqual(homework.follow_up_date("2026-07-31", 3),
                         "2026-08-03")
        self.assertEqual(homework.follow_up_date("2026-07-30", 1),
                         "2026-07-31")


class FollowupActionTest(unittest.TestCase):
    def test_wait_ask_expire(self):
        self.assertEqual(
            homework.followup_action("2026-08-03", "2026-07-31"), "wait")
        self.assertEqual(
            homework.followup_action("2026-07-31", "2026-07-31"), "ask")
        self.assertEqual(
            homework.followup_action("2026-07-28", "2026-07-31"), "ask")
        # follow_up から expire_days を超えたら掘り返さない
        self.assertEqual(
            homework.followup_action("2026-07-01", "2026-07-31",
                                     expire_days=14), "expire")


class SaveTest(HomeworkTestBase):
    def _digest_msgs(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで確認しとく", author=111)
            self._msg(conn, 11, "調べておくね", author=222)
        return [
            {"id": 10, "channel_id": CH, "author_id": 111,
             "content": "あとで確認しとく"},
            {"id": 11, "channel_id": CH, "author_id": 222,
             "content": "調べておくね"},
        ]

    def test_save_sets_owner_and_dates(self):
        msgs = self._digest_msgs()
        cands = [{"message_id": 10, "task": "見積もり確認"}]
        saved = homework.save_commitments(
            self.db_path, "agent1", cands, msgs, follow_up_days=3, now=NOW)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["owner"], "<@111>")
        self.assertEqual(saved[0]["follow_up_date"], "2026-08-03")
        with db.connect(self.db_path) as conn:
            rows = db.open_homework_due(conn, "agent1", "2026-08-03")
        self.assertEqual(rows[0]["committed_date"], "2026-07-31")
        self.assertEqual(rows[0]["task"], "見積もり確認")

    def test_save_is_idempotent_per_source(self):
        msgs = self._digest_msgs()
        cands = [{"message_id": 10, "task": "確認"}]
        self.assertEqual(len(homework.save_commitments(
            self.db_path, "agent1", cands, msgs, now=NOW)), 1)
        # 同じ発言は二重に追跡しない
        self.assertEqual(homework.save_commitments(
            self.db_path, "agent1", cands, msgs, now=NOW), [])

    def test_save_skips_unknown_author(self):
        msgs = [{"id": 12, "channel_id": CH, "author_id": None,
                 "content": "?"}]
        saved = homework.save_commitments(
            self.db_path, "agent1", [{"message_id": 12, "task": "x"}], msgs,
            now=NOW)
        self.assertEqual(saved, [])


class FollowupFlowTest(HomeworkTestBase):
    def _seed(self, *, follow="2026-08-03", status="open", task="確認",
              source=10):
        with db.connect(self.db_path) as conn:
            db.add_homework_item(
                conn, agent_id="agent1", source_message_id=source,
                channel_id=CH, owner="<@111>", task=task,
                committed_date="2026-07-31", follow_up_date=follow,
                created_at="2026-07-31T12:00")

    def test_items_needing_followup_partitions(self):
        self._seed(follow="2026-08-03", source=10)          # 期日ちょうど→ask
        self._seed(follow="2026-08-10", source=11)          # まだ先→対象外
        self._seed(follow="2026-07-10", source=12)          # 遅れすぎ→expire
        ask, expire = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-03", expire_days=14)
        self.assertEqual([i["source_message_id"] for i in ask], [10])
        self.assertEqual(len(expire), 1)

    def test_mark_asked_is_terminal(self):
        self._seed(follow="2026-08-03")
        ask, _ = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-03")
        homework.mark_asked(self.db_path, ask[0]["id"], 555)
        # 声かけ済みは二度と対象にならない
        again, _ = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-04")
        self.assertEqual(again, [])

    def test_mark_expired_removes_from_open(self):
        self._seed(follow="2026-07-10")
        _, expire = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-03")
        homework.mark_expired(self.db_path, expire)
        ask, expire2 = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-03")
        self.assertEqual((ask, expire2), ([], []))

    def test_build_followup_text(self):
        item = {"owner": "<@111>", "task": "見積もり確認", "channel_id": CH,
                "source_message_id": 42, "committed_date": "2026-07-31"}
        text = homework.build_followup_text(item, "1")
        self.assertIn("<@111>", text)
        self.assertIn("見積もり確認", text)
        self.assertIn("2026-07-31", text)
        self.assertIn("discord.com/channels/1/200/42", text)


if __name__ == "__main__":
    unittest.main()
