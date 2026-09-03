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


class ResolutionCheckTest(HomeworkTestBase):
    """トリガー発言の後の会話で既に完了した宿題を掘り起こしてしまう問題の
    回帰テスト。声かけ前に「その後のやりとり」を読み直す。"""

    ITEM = {"id": 1, "source_message_id": 10, "channel_id": CH,
            "owner": "<@111>", "task": "見積もりの確認"}

    def test_parse_resolution(self):
        self.assertTrue(homework.parse_resolution_response(
            '{"resolved": true}'))
        self.assertFalse(homework.parse_resolution_response(
            '{"resolved": false}'))
        # 壊れたJSON・判断不能は「未解決」扱い＝声かけは止めない
        self.assertFalse(homework.parse_resolution_response("無理でした"))
        self.assertFalse(homework.parse_resolution_response(""))

    def test_check_resolved_reads_later_messages(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            self._msg(conn, 11, "見積もり確認したよ。問題なかった")
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"resolved": true}'
        self.assertTrue(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=fake))
        self.assertIn("見積もり確認したよ", seen["prompt"])
        self.assertIn("見積もりの確認", seen["prompt"])
        # トリガー発言そのものは「その後のやりとり」に含めない
        self.assertNotIn("あとで見積もり確認しとく", seen["prompt"])

    def test_check_resolved_without_later_talk_skips_llm(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")

        def boom(_prompt):
            raise AssertionError("その後の会話が無ければclaudeを呼ばない")
        self.assertFalse(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=boom))

    def test_check_resolved_other_channel_ignored(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            self._msg(conn, 11, "別chの完了報告", channel=300)

        def boom(_prompt):
            raise AssertionError("同一chに後続が無ければclaudeを呼ばない")
        self.assertFalse(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=boom))

    def test_reactions_visible_in_prompt(self):
        # 「👍で完結」した会話が見えず掘り起こしていた回帰テスト
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            self._msg(conn, 11, "見積もり出しました", author=222)
            db.upsert_user(conn, id=333, name="u333",
                           display_name="人333", is_bot=False)
            db.add_reaction(conn, message_id=11, emoji="👍", user_id=333,
                            created_at="2026-08-23T10:00")
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"resolved": true}'
        self.assertTrue(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=fake))
        self.assertIn("👍(人333)", seen["prompt"])

    def test_source_reaction_alone_triggers_check(self):
        # 後続発言ゼロでも宿題発言そのものに✅が付いていれば確認にかける
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            db.upsert_user(conn, id=333, name="u333",
                           display_name="人333", is_bot=False)
            db.add_reaction(conn, message_id=10, emoji="✅", user_id=333,
                            created_at="2026-08-23T10:00")
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"resolved": true}'
        self.assertTrue(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=fake))
        self.assertIn("✅(人333)", seen["prompt"])

    def test_bot_reactions_hidden(self):
        # Botのリアクションは判断材料に混ぜない
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            self._msg(conn, 11, "進捗どうですか", author=222)
            db.upsert_user(conn, id=900, name="bot",
                           display_name="エージェント", is_bot=True)
            db.add_reaction(conn, message_id=11, emoji="👀", user_id=900,
                            created_at="2026-08-23T10:00")
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"resolved": false}'
        self.assertFalse(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=fake))
        self.assertNotIn("👀", seen["prompt"])

    def test_nudge_reactions_visible_in_prompt(self):
        # 声かけ（Bot投稿）への👍は後続会話に出てこないので別枠で読む
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            db.upsert_user(conn, id=333, name="u333",
                           display_name="担当者", is_bot=False)
            db.add_reaction(conn, message_id=900, emoji="👍", user_id=333,
                            created_at="2026-09-03T10:00")
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"resolved": true}'
        item = {**self.ITEM, "followup_message_id": 900}
        self.assertTrue(homework.check_resolved(
            self.db_path, item, invoke_fn=fake))
        self.assertIn("声かけ", seen["prompt"])
        self.assertIn("👍(担当者)", seen["prompt"])

    def test_check_resolved_error_falls_back_to_ask(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "あとで見積もり確認しとく")
            self._msg(conn, 11, "見積もりの件、終わりました")

        def broken(_prompt):
            raise RuntimeError("claude down")
        # 確認に失敗したら従来どおり声かけする（機能を黙って殺さない）
        self.assertFalse(homework.check_resolved(
            self.db_path, self.ITEM, invoke_fn=broken))

    def test_mark_resolved_is_terminal(self):
        with db.connect(self.db_path) as conn:
            db.add_homework_item(
                conn, agent_id="agent1", source_message_id=10,
                channel_id=CH, owner="<@111>", task="確認",
                committed_date="2026-07-31", follow_up_date="2026-08-03",
                created_at="2026-07-31T12:00")
        ask, _ = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-03")
        homework.mark_resolved(self.db_path, ask[0]["id"])
        ask2, _ = homework.items_needing_followup(
            self.db_path, "agent1", "2026-08-03")
        self.assertEqual(ask2, [])


class StageTest(HomeworkTestBase):
    """催促後の出口。asked のまま長期滞留していた問題。
    二度目の催促→手放し、の遷移と、溜まった分の分散を検証する。"""

    def _seed(self, source, status, asked_at):
        with db.connect(self.db_path) as conn:
            db.add_homework_item(
                conn, agent_id="agent1", source_message_id=source,
                channel_id=CH, owner="<@111>", task=f"宿題{source}",
                committed_date="2026-08-20", follow_up_date="2026-08-23",
                created_at="2026-08-20T12:00")
            iid = conn.execute(
                "SELECT id FROM homework_items WHERE source_message_id=?",
                (source,)).fetchone()[0]
            db.set_homework_status(conn, iid, status,
                                   followup_message_id=900 + source,
                                   asked_at=asked_at)
        return iid

    def test_stage_action_transitions(self):
        now = datetime(2026, 9, 3, 12, 0)
        asked = {"status": "asked", "asked_at": "2026-09-01T12:00"}
        self.assertEqual(homework.stage_action(asked, now), "wait")
        asked = {"status": "asked", "asked_at": "2026-08-30T12:00"}
        self.assertEqual(homework.stage_action(asked, now), "nudge2")
        asked2 = {"status": "asked2", "asked_at": "2026-09-01T12:00"}
        self.assertEqual(homework.stage_action(asked2, now), "wait")
        asked2 = {"status": "asked2", "asked_at": "2026-08-30T12:00"}
        self.assertEqual(homework.stage_action(asked2, now), "close")
        # 声かけ時刻が無い旧データは触らない
        self.assertEqual(homework.stage_action(
            {"status": "asked", "asked_at": None}, now), "wait")

    def test_items_needing_stage_partitions_and_limits(self):
        now = datetime(2026, 9, 3, 12, 0)
        self._seed(1, "asked", "2026-08-25T12:00")    # 二度目の対象
        self._seed(2, "asked", "2026-09-02T12:00")    # まだ待つ
        self._seed(3, "asked2", "2026-08-25T12:00")   # 手放しの対象
        self._seed(4, "asked", "2026-08-26T12:00")    # 二度目の対象（3件目）
        nudge2, close = homework.items_needing_stage(
            self.db_path, "agent1", now, limit=2)
        # 合計2件までに絞られ、古い順に拾う
        self.assertEqual(len(nudge2) + len(close), 2)
        picked = sorted(i["source_message_id"] for i in nudge2 + close)
        self.assertEqual(picked, [1, 3])

    def test_mark_asked_records_time_and_second_stage(self):
        iid = self._seed(1, "asked", "2026-08-25T12:00")
        homework.mark_asked(self.db_path, iid, 1234, status="asked2",
                            now=datetime(2026, 9, 3, 12, 0))
        with db.connect(self.db_path) as conn:
            rows = db.staged_homework(conn, "agent1")
        self.assertEqual(rows[0]["status"], "asked2")
        self.assertEqual(rows[0]["asked_at"], "2026-09-03T12:00")
        self.assertEqual(rows[0]["followup_message_id"], 1234)
        homework.mark_closed(self.db_path, iid, 5678)
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.staged_homework(conn, "agent1"), [])

    def test_texts(self):
        item = {"owner": "<@111>", "task": "見積もり確認", "channel_id": CH,
                "source_message_id": 42}
        second = homework.build_second_nudge_text(item, "1")
        self.assertIn("もう一回だけ", second)
        self.assertIn("👍", second)
        self.assertIn("discord.com/channels/1/200/42", second)
        close = homework.build_close_text(item)
        self.assertIn("手放し", close)
        self.assertNotIn("<@111>", close)   # 手放しは鳴らさない


class MigrationTest(HomeworkTestBase):
    def test_asked_at_backfilled_for_legacy_rows(self):
        with db.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE homework_items RENAME TO hw_old")
            conn.execute(
                """CREATE TABLE homework_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT,
                    source_message_id INTEGER, channel_id INTEGER, owner TEXT,
                    task TEXT, committed_date TEXT, follow_up_date TEXT,
                    status TEXT DEFAULT 'open', followup_message_id INTEGER,
                    created_at TEXT, UNIQUE(agent_id, source_message_id))""")
            conn.execute(
                """INSERT INTO homework_items(agent_id, source_message_id,
                       channel_id, owner, task, committed_date, follow_up_date,
                       status, followup_message_id, created_at)
                   VALUES('agent1', 1, 200, '<@1>', 't', '2026-08-20',
                          '2026-08-23', 'asked', 900, '2026-08-20T12:00')""")
        db.init_db(self.db_path)   # migration が走る
        with db.connect(self.db_path) as conn:
            rows = db.staged_homework(conn, "agent1")
        self.assertEqual(rows[0]["asked_at"], "2026-08-23T00:00")


if __name__ == "__main__":
    unittest.main()
