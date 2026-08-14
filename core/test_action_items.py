#!/usr/bin/env python3
"""議事録の納期追跡（action_items / エージェントv3 Phase B）のユニットテスト。

検知（Webhook投稿のみ・初回初期化）・抽出JSONの検証（期日を発明しない）・
声かけ段階の遷移・✅完了・❌一括取り消しを検証する。claude は invoke_fn 注入。
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import action_items
from core import db

NOW = datetime(2026, 7, 31, 12, 0)
MINUTES_CH = 555


class ActionItemsTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _minutes_msg(self, conn, mid, text, *, is_bot=True, author=900):
        db.upsert_channel(conn, id=MINUTES_CH, name="ai議事録", type="text")
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name="議事録BOT" if is_bot else "人間",
                       is_bot=is_bot)
        db.insert_message(conn, id=mid, channel_id=MINUTES_CH,
                          author_id=author, content=text,
                          created_at="2026-07-31T10:00:00+00:00")

    def _seed_item(self, *, due="2026-08-02", stage="none", status="open",
                   task="発注を進める", urgent=False):
        with db.connect(self.db_path) as conn:
            iid = db.add_action_item(
                conn, agent_id="agent1", source_message_id=1,
                channel_id=MINUTES_CH, task=task, owners="<@111>",
                due_date=due, urgent=urgent, created_at="2026-07-31T10:00")
            if stage != "none":
                db.update_action_nudge(conn, iid, stage=stage, message_id=77)
            if status != "open":
                conn.execute("UPDATE action_items SET status=? WHERE id=?",
                             (status, iid))
        return iid


class CollectMinutesTest(ActionItemsTestBase):
    def test_first_run_initializes_silently(self):
        with db.connect(self.db_path) as conn:
            self._minutes_msg(conn, 1, "☐ TODO: 何か（担当: <@1>）")
        self.assertIsNone(action_items.collect_new_minutes(
            self.db_path, "agent1", MINUTES_CH, now=NOW))
        with db.connect(self.db_path) as conn:
            self._minutes_msg(conn, 2, "📋 定例会議 2026/07/31")
            self._minutes_msg(conn, 3, "🔴 TODO: 急ぎ（担当: <@2>）")
            self._minutes_msg(conn, 4, "人間の感想", is_bot=False, author=1)
        batch = action_items.collect_new_minutes(
            self.db_path, "agent1", MINUTES_CH, now=NOW)
        self.assertEqual(batch["header_id"], 2)  # 初回以降の新着のみ・Botのみ
        self.assertIn("定例会議", batch["text"])
        self.assertIn("急ぎ", batch["text"])
        self.assertNotIn("人間の感想", batch["text"])
        self.assertEqual(batch["date"], "2026-07-31")  # UTC→JST日付
        # checkpoint前進済み → 次回は新着なし
        self.assertIsNone(action_items.collect_new_minutes(
            self.db_path, "agent1", MINUTES_CH, now=NOW))


class ParseExtractTest(unittest.TestCase):
    def test_valid_items(self):
        raw = ('{"items": [{"task": "発注", "owners": ["<@111>", "<@222>"], '
               '"due": "2026-08-29", "urgent": true}]}')
        out = action_items.parse_extract_response(raw, "2026-07-31")
        self.assertEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertEqual(item["owners"], "<@111> <@222>")
        self.assertEqual(item["due_date"], "2026-08-29")
        self.assertTrue(item["urgent"])

    def test_no_due_is_skipped_not_invented(self):
        raw = ('{"items": [{"task": "急ぎ確認", "owners": ["<@1>"], '
               '"due": null, "urgent": true}, {"task": "できるだけ早く", '
               '"owners": ["<@1>"], "due": "できるだけ早く"}]}')
        out = action_items.parse_extract_response(raw, "2026-07-31")
        self.assertEqual(out["items"], [])
        self.assertEqual(out["skipped_no_due"], 2)

    def test_invalid_owner_or_past_due_dropped(self):
        raw = ('{"items": ['
               '{"task": "宛先不正", "owners": ["@みんな"], "due": "2026-08-01"},'
               '{"task": "過去日", "owners": ["<@1>"], "due": "2026-07-01"},'
               '{"task": "宛先なし", "owners": [], "due": "2026-08-01"}]}')
        out = action_items.parse_extract_response(raw, "2026-07-31")
        self.assertEqual(out["items"], [])

    def test_broken_json(self):
        out = action_items.parse_extract_response("抽出できません", "2026-07-31")
        self.assertEqual(out, {"items": [], "skipped_no_due": 0})

    def test_extract_uses_invoke_fn_with_date(self):
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"items": []}'

        action_items.extract_items("☐ TODO: x（担当: <@1>）", "2026-07-31",
                                   invoke_fn=fake)
        self.assertIn("2026-07-31", seen["prompt"])
        self.assertIn("☐ TODO: x", seen["prompt"])


class NudgeStageTest(unittest.TestCase):
    def test_desired_stage(self):
        self.assertEqual(action_items.desired_stage("2026-08-10", "2026-07-31"),
                         "none")
        self.assertEqual(action_items.desired_stage("2026-08-02", "2026-07-31"),
                         "before")  # 2日前
        self.assertEqual(action_items.desired_stage("2026-08-01", "2026-07-31"),
                         "before")  # 1日前もbefore扱い
        self.assertEqual(action_items.desired_stage("2026-07-31", "2026-07-31"),
                         "day")
        self.assertEqual(action_items.desired_stage("2026-07-30", "2026-07-31"),
                         "overdue")


class NudgeFlowTest(ActionItemsTestBase):
    def test_items_needing_nudge_progression(self):
        iid = self._seed_item(due="2026-08-02")  # 今日=7/31 → before対象
        due = action_items.items_needing_nudge(self.db_path, "agent1",
                                               "2026-07-31")
        self.assertEqual([(i["id"], s) for i, s in due], [(iid, "before")])
        action_items.record_nudge(self.db_path, iid, "before", 999)
        # 同じ日はもう声かけしない
        self.assertEqual(action_items.items_needing_nudge(
            self.db_path, "agent1", "2026-07-31"), [])
        # 当日になったら day 段階へ進む
        due = action_items.items_needing_nudge(self.db_path, "agent1",
                                               "2026-08-02")
        self.assertEqual([(i["id"], s) for i, s in due], [(iid, "day")])

    def test_done_and_dropped_are_not_nudged(self):
        self._seed_item(due="2026-07-31", status="done")
        self._seed_item(due="2026-07-31", status="dropped")
        self.assertEqual(action_items.items_needing_nudge(
            self.db_path, "agent1", "2026-07-31"), [])

    def test_nudge_text_mentions_owner_and_link(self):
        item = {"id": 1, "channel_id": MINUTES_CH, "source_message_id": 42,
                "task": "発注を進める", "owners": "<@111>",
                "due_date": "2026-08-02"}
        text = action_items.build_nudge_text(item, "before", "1")
        self.assertIn("<@111>", text)
        self.assertIn("発注を進める", text)
        self.assertIn("discord.com/channels/1/555/42", text)
        self.assertIn("✅", text)

    def test_complete_by_nudge_message(self):
        iid = self._seed_item(stage="day")
        item = action_items.complete_by_nudge_message(self.db_path, 77)
        self.assertEqual(item["id"], iid)
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.open_action_items(conn, "agent1"), [])
        # 二重✅は空振り
        self.assertIsNone(
            action_items.complete_by_nudge_message(self.db_path, 77))

    def test_drop_by_confirm_message(self):
        ids = [self._seed_item(), self._seed_item(task="別タスク")]
        action_items.set_confirm_message(self.db_path, ids, 500)
        self.assertEqual(
            action_items.drop_by_confirm_message(self.db_path, 500), 2)
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.open_action_items(conn, "agent1"), [])


class ConfirmationTest(unittest.TestCase):
    def test_build_confirmation(self):
        items = [{"task": "発注", "owners": "<@1>", "due_date": "2026-08-29",
                  "urgent": True},
                 {"task": "確認", "owners": "<@2>", "due_date": "2026-08-01",
                  "urgent": False}]
        text = action_items.build_confirmation(items, skipped_no_due=1)
        self.assertIn("2件を追跡", text)
        self.assertIn("🔴 発注", text)
        self.assertIn("期日 2026-08-01", text)
        self.assertIn("1件は追跡対象外", text)
        self.assertIn("❌", text)


class ConversationMarkerTest(unittest.TestCase):
    def test_markers_extracted_and_removed(self):
        text, cancels, dones = action_items.extract_conversation_markers(
            "了解っス\n[ACTION_CANCEL: 3]\n[ACTION_DONE: 5]")
        self.assertEqual(text, "了解っス")
        self.assertEqual(cancels, [3])
        self.assertEqual(dones, [5])

    def test_no_markers_returns_text_as_is(self):
        text, cancels, dones = action_items.extract_conversation_markers(
            "普通の返事っス")
        self.assertEqual((text, cancels, dones), ("普通の返事っス", [], []))

    def test_none_answer_is_safe(self):
        self.assertEqual(action_items.extract_conversation_markers(None),
                         ("", [], []))


class SkillNoteTest(unittest.TestCase):
    def test_lists_open_items_with_ids(self):
        note = action_items.build_skill_note(
            [{"id": 1, "task": "撮影機材の確認", "due_date": "2026-08-08",
              "owners": "<@111>"}])
        self.assertIn("id=1: 撮影機材の確認", note)
        self.assertIn("[ACTION_CANCEL: id]", note)
        self.assertIn("[ACTION_DONE: id]", note)
        # 一覧＝真実（会話履歴に基づく「キャンセル済み」の作話防止）
        self.assertIn("一覧だけを真実として扱う", note)

    def test_empty_list_says_none(self):
        note = action_items.build_skill_note([])
        self.assertIn("（なし）", note)
        # できない依頼を引き受けない指示は常に入る
        self.assertIn("できないと正直に答える", note)


class ConversationOpsTest(ActionItemsTestBase):
    def _status(self, iid):
        with db.connect(self.db_path) as conn:
            return db.get_action_item(conn, iid, "agent1")["status"]

    def test_owner_can_cancel(self):
        iid = self._seed_item()
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id="111", is_admin=False,
            cancel_ids=[iid], done_ids=[])
        self.assertEqual(self._status(iid), "cancelled")
        self.assertIn("🗑 納期追跡をキャンセル", notes[0])
        self.assertEqual(applied[0]["action"], "cancel")

    def test_admin_can_complete(self):
        iid = self._seed_item()
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id="999", is_admin=True,
            cancel_ids=[], done_ids=[iid])
        self.assertEqual(self._status(iid), "done")
        self.assertIn("📗 納期追跡を完了として記録", notes[0])
        self.assertEqual(applied[0]["action"], "done")

    def test_stranger_is_refused(self):
        iid = self._seed_item()
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id="999", is_admin=False,
            cancel_ids=[iid], done_ids=[])
        self.assertEqual(self._status(iid), "open")
        self.assertIn("⚠️ 納期追跡", notes[0])
        self.assertEqual(applied, [])

    def test_unknown_id_and_closed_item(self):
        iid = self._seed_item(status="done")
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id="111", is_admin=True,
            cancel_ids=[404, iid], done_ids=[])
        self.assertIn("id=404 は無いっス", notes[0])
        self.assertIn("既に完了済み", notes[1])
        self.assertEqual(applied, [])

    def test_other_agents_items_are_invisible(self):
        iid = self._seed_item()
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "ayako", author_id="111", is_admin=True,
            cancel_ids=[iid], done_ids=[])
        self.assertIn("無いっス", notes[0])
        self.assertEqual(self._status(iid), "open")


if __name__ == "__main__":
    unittest.main()
