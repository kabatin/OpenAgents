#!/usr/bin/env python3
"""朝のブリーフィング（briefing / エージェントv3・RM#42）のユニットテスト。

配信タイミング（時刻ゲート＋1日1回）・期日の区分（超過/今日/近日）・今日の予定の
抽出・重要事項の要約JSONの検証（best-effort・迷ったら捨てる）・集約（初回初期化と
checkpoint前進）・本文整形（空なら None・鳴らさない前提）を検証する。
claude は invoke_fn 注入で呼ばない。
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import briefing
from core import db

NOW = datetime(2026, 7, 31, 9, 0)   # 金曜9時（配信時刻8時を過ぎている）
TODAY = "2026-07-31"
HOME_CH = 100
CH = 200


# ---------------------------------------------------------------- 純粋関数

class DueTodayTest(unittest.TestCase):
    def test_before_hour_is_false(self):
        self.assertFalse(briefing.due_today(
            datetime(2026, 7, 31, 7, 0), None, 8))

    def test_after_hour_never_sent_is_true(self):
        self.assertTrue(briefing.due_today(NOW, None, 8))

    def test_already_sent_today_is_false(self):
        self.assertFalse(briefing.due_today(NOW, "2026-07-31T08:05", 8))

    def test_sent_yesterday_is_true(self):
        self.assertTrue(briefing.due_today(NOW, "2026-07-30T08:05", 8))

    def test_broken_last_run_falls_back_to_send(self):
        self.assertTrue(briefing.due_today(NOW, "こわれた", 8))


class PartitionDeadlinesTest(unittest.TestCase):
    def _items(self):
        return [
            {"due_date": "2026-07-29", "task": "超過A"},   # overdue
            {"due_date": "2026-07-31", "task": "今日B"},   # today
            {"due_date": "2026-08-01", "task": "近日C"},   # soon (+1)
            {"due_date": "2026-08-02", "task": "近日D"},   # soon (+2)
            {"due_date": "2026-08-10", "task": "遠いE"},   # 対象外
            {"due_date": "", "task": "期日なし"},           # 対象外
        ]

    def test_partition(self):
        out = briefing.partition_deadlines(self._items(), TODAY)
        self.assertEqual([i["task"] for i in out["overdue"]], ["超過A"])
        self.assertEqual([i["task"] for i in out["today"]], ["今日B"])
        self.assertEqual([i["task"] for i in out["soon"]], ["近日C", "近日D"])


class RemindersDueTodayTest(unittest.TestCase):
    def test_filters_by_date(self):
        active = [
            {"due": "2026-07-31T09:00", "content": "今日の予定"},
            {"due": "2026-07-31T18:30", "content": "夕方も今日"},
            {"due": "2026-08-01T09:00", "content": "明日"},
        ]
        out = briefing.reminders_due_today(active, TODAY)
        self.assertEqual([r["content"] for r in out], ["今日の予定", "夕方も今日"])


class ParseHighlightTest(unittest.TestCase):
    def test_valid(self):
        raw = '前 {"highlights": ["Aを決定", "Bの締切は金曜"]} 後'
        self.assertEqual(briefing.parse_highlight_response(raw),
                         ["Aを決定", "Bの締切は金曜"])

    def test_empty_and_broken(self):
        self.assertEqual(
            briefing.parse_highlight_response('{"highlights": []}'), [])
        self.assertEqual(briefing.parse_highlight_response("該当なし"), [])
        self.assertEqual(briefing.parse_highlight_response(""), [])

    def test_non_string_dropped_and_capped(self):
        raw = ('{"highlights": ["ok1", 123, {"x": 1}, "  ", '
               '"ok2", "ok3", "ok4"]}')
        out = briefing.parse_highlight_response(raw)
        self.assertEqual(out, ["ok1", "ok2", "ok3"])
        self.assertLessEqual(len(out), briefing.MAX_HIGHLIGHTS)


class SummarizeHighlightsTest(unittest.TestCase):
    MSGS = [{"channel": "ch", "author": "太郎", "content": "締切決めた"}]

    def test_empty_messages_skips_llm(self):
        called = []
        self.assertEqual(briefing.summarize_highlights(
            [], agent_name="アーカイブ担当",
            invoke_fn=lambda p: called.append(p) or "x"), [])
        self.assertEqual(called, [])

    def test_uses_invoke_fn(self):
        seen = {}

        def fake(prompt):
            seen["p"] = prompt
            return '{"highlights": ["締切決定"]}'

        out = briefing.summarize_highlights(
            self.MSGS, agent_name="アーカイブ担当", invoke_fn=fake)
        self.assertEqual(out, ["締切決定"])
        self.assertIn("締切決めた", seen["p"])
        self.assertIn("アーカイブ担当", seen["p"])

    def test_llm_failure_degrades_to_empty(self):
        def boom(prompt):
            raise RuntimeError("timeout")

        self.assertEqual(briefing.summarize_highlights(
            self.MSGS, agent_name="アーカイブ担当", invoke_fn=boom), [])


class BuildBriefingTest(unittest.TestCase):
    def _dl(self):
        return {"overdue": [{"task": "超過タスク", "owners": "<@111>",
                             "channel_id": CH, "source_message_id": 9,
                             "due_date": "2026-07-29", "urgent": True}],
                "today": [{"task": "今日タスク", "owners": "<@222>",
                           "channel_id": CH, "source_message_id": 10,
                           "due_date": TODAY, "urgent": False}],
                "soon": []}

    def test_empty_returns_none(self):
        self.assertIsNone(briefing.build_briefing(
            date_label="07/31(金)", deadlines={"overdue": [], "today": [],
                                               "soon": []},
            schedules=[], highlights=[], guild_id="1"))

    def test_sections_present(self):
        text = briefing.build_briefing(
            date_label="07/31(金)", deadlines=self._dl(),
            schedules=[{"due": "2026-07-31T09:00", "content": "定例会議",
                        "mention_label": None}],
            highlights=["Aの締切が近い"], guild_id="1")
        assert text is not None
        self.assertIn("07/31(金)", text)
        self.assertIn("今日の期日", text)
        self.assertIn("超過タスク", text)
        self.assertIn("🔥", text)                       # urgent マーカー
        self.assertIn("<@222>", text)                  # 担当は表示される
        self.assertIn("今日の予定", text)
        self.assertIn("09:00 定例会議", text)
        self.assertIn("未読の重要事項", text)
        self.assertIn("Aの締切が近い", text)
        self.assertIn("discord.com/channels/1/200/10", text)   # ジャンプリンク

    def test_only_schedules(self):
        text = briefing.build_briefing(
            date_label="07/31(金)",
            deadlines={"overdue": [], "today": [], "soon": []},
            schedules=[{"due": "2026-07-31T12:00", "content": "ランチ"}],
            highlights=[], guild_id="1")
        assert text is not None
        self.assertNotIn("今日の期日", text)
        self.assertIn("今日の予定", text)

    def test_length_guard(self):
        many = [{"task": "x" * 60, "owners": "<@1>", "channel_id": CH,
                 "source_message_id": i, "due_date": TODAY, "urgent": False}
                for i in range(50)]
        text = briefing.build_briefing(
            date_label="07/31(金)",
            deadlines={"overdue": [], "today": many, "soon": []},
            schedules=[], highlights=[], guild_id="1")
        assert text is not None
        self.assertLessEqual(len(text), briefing.MAX_BRIEFING_CHARS + 1)


# ---------------------------------------------------------------- IO（DB）

class BriefingDbBase(unittest.TestCase):
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
                          content=text, created_at="2026-07-31T00:00:00+00:00")

    def _action(self, conn, *, task, due, source=10, urgent=False):
        return db.add_action_item(
            conn, agent_id="agent1", source_message_id=source, channel_id=CH,
            task=task, owners="<@111>", due_date=due, urgent=urgent,
            created_at="2026-07-31T00:00")


class ShouldSendMarkTest(BriefingDbBase):
    def test_send_once_per_day(self):
        # 初回（state無し・9時）は配信対象
        self.assertTrue(briefing.should_send(
            self.db_path, "agent1", hour=8, now=NOW))
        briefing.mark_sent(self.db_path, "agent1", 5, now=NOW)
        # 同日中はもう送らない
        self.assertFalse(briefing.should_send(
            self.db_path, "agent1", hour=8, now=NOW))
        # 翌日は再び送る
        self.assertTrue(briefing.should_send(
            self.db_path, "agent1", hour=8, now=datetime(2026, 8, 1, 9, 0)))


class CollectTest(BriefingDbBase):
    def test_first_run_has_no_unread_and_sets_checkpoint(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "過去の発言")
            self._action(conn, task="今日の期日", due=TODAY)
        data = briefing.collect(
            self.db_path, "agent1", home_channel_id=HOME_CH,
            now=NOW, list_reminders=lambda: [])
        # 初回は未読を遡らない。期日はちゃんと拾う
        self.assertEqual(data["unread"], [])
        self.assertEqual(data["checkpoint"], 1)
        self.assertEqual([i["task"] for i in data["deadlines"]["today"]],
                         ["今日の期日"])

    def test_unread_after_checkpoint_excludes_home_and_bot(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1, "初期化前")
        briefing.mark_sent(self.db_path, "agent1", 1, now=NOW)
        with db.connect(self.db_path) as conn:
            self._msg(conn, 2, "作業chの重要発言", channel=CH)
            self._msg(conn, 3, "botの発言", author=900, is_bot=True)
            self._msg(conn, 4, "ホームchの雑談", channel=HOME_CH)
            self._msg(conn, 5, "除外chの発言", channel=300)
        data = briefing.collect(
            self.db_path, "agent1", home_channel_id=HOME_CH,
            exclude_channel_ids=[300], now=NOW, list_reminders=lambda: [])
        self.assertEqual([m["id"] for m in data["unread"]], [2])
        self.assertEqual(data["checkpoint"], 5)

    def test_schedules_come_from_injected_reminders(self):
        data = briefing.collect(
            self.db_path, "agent1", home_channel_id=HOME_CH, now=NOW,
            list_reminders=lambda: [
                {"due": "2026-07-31T10:00", "content": "今日"},
                {"due": "2026-08-05T10:00", "content": "来週"}])
        self.assertEqual([r["content"] for r in data["schedules"]], ["今日"])


if __name__ == "__main__":
    unittest.main()
