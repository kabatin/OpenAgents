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
        text, cancels, dones, dues = action_items.extract_conversation_markers(
            "了解です\n[ACTION_CANCEL: 3]\n[ACTION_DONE: 5]")
        self.assertEqual(text, "了解です")
        self.assertEqual(cancels, [3])
        self.assertEqual(dones, [5])
        self.assertEqual(dues, [])

    def test_no_markers_returns_text_as_is(self):
        got = action_items.extract_conversation_markers("普通の返事です")
        self.assertEqual(got, ("普通の返事です", [], [], []))

    def test_none_answer_is_safe(self):
        self.assertEqual(action_items.extract_conversation_markers(None),
                         ("", [], [], []))


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
        self.assertIn("id=404 はありません", notes[0])
        self.assertIn("既に完了済み", notes[1])
        self.assertEqual(applied, [])

    def test_other_agents_items_are_invisible(self):
        iid = self._seed_item()
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "ayako", author_id="111", is_admin=True,
            cancel_ids=[iid], done_ids=[])
        self.assertIn("ありません", notes[0])
        self.assertEqual(self._status(iid), "open")


if __name__ == "__main__":
    unittest.main()


class RescheduleTest(unittest.TestCase):
    """期日変更の会話導線（2026-08-19）。取消・完了はあるのに変更が無く、
    「金曜にリスケされた」が事実台帳へ流れ込んでいた。"""

    OWNER = "857938052860608523"

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        with db.connect(self.db_path) as conn:
            self.item_id = db.add_action_item(
                conn, agent_id="agent1", source_message_id=1, channel_id=7,
                task="購入フロー一式をデバッグ", owners=f"<@{self.OWNER}>",
                due_date="2026-08-17", urgent=False, created_at="t")
            # 超過まで声かけ済みの状態にしておく
            db.update_action_nudge(conn, self.item_id, stage="overdue",
                                   message_id=99)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_marker_extraction(self):
        text, cancels, dones, dues = action_items.extract_conversation_markers(
            "金曜にリスケしますね\n[ACTION_DUE: 4 | 2026-08-21]")
        self.assertEqual(text, "金曜にリスケしますね")
        self.assertEqual(dues, [(4, "2026-08-21")])
        self.assertEqual((cancels, dones), ([], []))

    def test_malformed_date_ignored_but_removed(self):
        text, _c, _d, dues = action_items.extract_conversation_markers(
            "本文[ACTION_DUE: 4 | 8/21]")
        self.assertEqual(dues, [])
        self.assertIn("本文", text)   # 不正でも本文は残る

    def test_reschedule_resets_nudge_stage(self):
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id=self.OWNER, is_admin=False,
            cancel_ids=[], done_ids=[],
            due_changes=[(self.item_id, "2026-08-21")])
        self.assertIn("📅 納期追跡の期日を変更", notes[0])
        self.assertIn("2026-08-17 → 2026-08-21", notes[0])
        self.assertEqual(applied[0]["action"], "due")
        with db.connect(self.db_path) as conn:
            item = db.get_action_item(conn, self.item_id, "agent1")
        self.assertEqual(item["due_date"], "2026-08-21")
        self.assertEqual(item["nudge_stage"], "none")   # 声かけをやり直す
        self.assertEqual(item["status"], "open")

    def test_non_owner_rejected(self):
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id="999", is_admin=False,
            cancel_ids=[], done_ids=[],
            due_changes=[(self.item_id, "2026-08-21")])
        self.assertIn("担当の人か管理者", notes[0])
        self.assertEqual(applied, [])

    def test_admin_can_reschedule(self):
        _notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id="999", is_admin=True,
            cancel_ids=[], done_ids=[],
            due_changes=[(self.item_id, "2026-08-21")])
        self.assertEqual(len(applied), 1)

    def test_closed_item_not_rescheduled(self):
        with db.connect(self.db_path) as conn:
            db.close_action_item(conn, self.item_id, "agent1", status="done")
        notes, applied = action_items.apply_conversation_ops(
            self.db_path, "agent1", author_id=self.OWNER, is_admin=False,
            cancel_ids=[], done_ids=[],
            due_changes=[(self.item_id, "2026-08-21")])
        self.assertIn("既に完了済み", notes[0])
        self.assertEqual(applied, [])

    def test_skill_note_offers_due_change_and_forbids_fact(self):
        with db.connect(self.db_path) as conn:
            items = db.open_action_items(conn, "agent1")
        note = action_items.build_skill_note(items)
        self.assertIn("[ACTION_DUE: id | YYYY-MM-DD]", note)
        self.assertIn("[FACT:]（事実台帳）に書かないこと", note)
        self.assertNotIn("期日・担当の変更はできない", note)


class ChannelEstimateTest(unittest.TestCase):
    """議事録TODOが全件 議事録ch 紐づきで、実際の作業chから見えなかった問題。
    抽出時に「作業が進むch」を候補一覧から推定させる。"""
    CHANNELS = [(701, "グッズ総合"), (702, "ショップ開発")]
    RAW = ('{"items": ['
           '{"task": "タンブラー試作発注", "owners": ["<@1>"], '
           '"due": "2026-09-10", "channel": "#グッズ総合"},'
           '{"task": "課金テスト", "owners": ["<@2>"], '
           '"due": "2026-09-11", "channel": "存在しないch"},'
           '{"task": "chなし", "owners": ["<@3>"], "due": "2026-09-12"}]}')

    def test_prompt_lists_candidates_only_when_given(self):
        p = action_items.build_extract_prompt("本文", "2026-09-03",
                                              channels=self.CHANNELS)
        self.assertIn("#グッズ総合、#ショップ開発", p)
        self.assertIn('"channel"', p)
        p0 = action_items.build_extract_prompt("本文", "2026-09-03")
        self.assertNotIn("channel", p0)

    def test_parse_maps_name_to_id_and_falls_back(self):
        out = action_items.parse_extract_response(
            self.RAW, "2026-09-03", channels=self.CHANNELS)
        by_task = {it["task"]: it["channel_id"] for it in out["items"]}
        self.assertEqual(by_task["タンブラー試作発注"], 701)
        self.assertIsNone(by_task["課金テスト"])   # 一覧に無い名前は倒す
        self.assertIsNone(by_task["chなし"])

    def test_save_uses_estimated_channel_or_minutes(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db.init_db(path)
            items = action_items.parse_extract_response(
                self.RAW, "2026-09-03", channels=self.CHANNELS)["items"]
            action_items.save_items(path, "agent1", MINUTES_CH, 42, items)
            with db.connect(path) as conn:
                rows = {r["task"]: r["channel_id"]
                        for r in db.open_action_items(conn, "agent1")}
            self.assertEqual(rows["タンブラー試作発注"], 701)
            self.assertEqual(rows["課金テスト"], MINUTES_CH)
        finally:
            os.unlink(path)


class ChannelCandidatesTest(unittest.TestCase):
    def test_human_channels_only_with_exclusion(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db.init_db(path)
            with db.connect(path) as conn:
                for cid in (701, 702, 703):
                    db.upsert_channel(conn, id=cid, name=f"ch{cid}", type="text")
                db.upsert_user(conn, id=1, name="u1", display_name="人",
                               is_bot=False)
                db.upsert_user(conn, id=9, name="bot", display_name="bot",
                               is_bot=True)
                now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
                db.insert_message(conn, id=1, channel_id=701, author_id=1,
                                  content="a", created_at=now)
                db.insert_message(conn, id=2, channel_id=701, author_id=1,
                                  content="b", created_at=now)
                db.insert_message(conn, id=3, channel_id=702, author_id=1,
                                  content="c", created_at=now)
                db.insert_message(conn, id=4, channel_id=703, author_id=9,
                                  content="bot only", created_at=now)
                self.assertEqual(
                    db.channel_candidates(conn), [(701, "ch701"), (702, "ch702")])
                self.assertEqual(
                    db.channel_candidates(conn, exclude_channel_ids={"701"}),
                    [(702, "ch702")])
        finally:
            os.unlink(path)
