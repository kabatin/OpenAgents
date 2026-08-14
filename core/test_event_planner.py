#!/usr/bin/env python3
"""イベント逆算スケジュール（event_planner / RM#35）のユニットテスト。

検知（開催キーワード×日付・年補完）・逆算案の検証（範囲外日付の破棄）・
提案文・✅承認→action_items登録・❌見送り・CAS排他を検証する。
"""

import os
import tempfile
import unittest
from datetime import date

from core import db
from core import event_planner


TODAY = date(2026, 8, 1)


class DetectTest(unittest.TestCase):
    def test_detects_event_with_date(self):
        rows = [{"id": 1, "decision": "サマーカップは8/29に開催する",
                 "channel_id": 5, "source_message_id": 10,
                 "decided_on": "2026-07-24"}]
        out = event_planner.detect_events(rows, TODAY)
        self.assertEqual(out[0]["event_date"], "2026-08-29")
        self.assertEqual(out[0]["decision_id"], 1)

    def test_year_rollover_and_formats(self):
        self.assertEqual(event_planner.parse_event_date("3/15に開催", TODAY),
                         "2027-03-15")   # 過去日は翌年に解釈
        self.assertEqual(
            event_planner.parse_event_date("2026-09-01 実施", TODAY),
            "2026-09-01")
        self.assertEqual(
            event_planner.parse_event_date("9月5日開催", TODAY), "2026-09-05")

    def test_ignores_non_event_or_dateless(self):
        rows = [{"id": 1, "decision": "月謝は10万円に決定", "channel_id": 5,
                 "source_message_id": 10, "decided_on": "t"},
                {"id": 2, "decision": "イベントの方針は継続検討",
                 "channel_id": 5, "source_message_id": 11, "decided_on": "t"}]
        self.assertEqual(event_planner.detect_events(rows, TODAY), [])


class PlanParseTest(unittest.TestCase):
    def test_valid_milestones_sorted(self):
        raw = ('{"milestones": ['
               '{"task": "告知開始", "due": "2026-08-19", "owners": []},'
               '{"task": "グッズ発注", "due": "2026-08-08", '
               '"owners": ["<@123>"]}]}')
        out = event_planner.parse_plan(raw, "2026-08-01", "2026-08-29")
        self.assertEqual([m["task"] for m in out], ["グッズ発注", "告知開始"])
        self.assertEqual(out[0]["owners"], "<@123>")
        self.assertEqual(out[1]["owners"], "（担当未定）")

    def test_out_of_range_dates_dropped(self):
        raw = ('{"milestones": ['
               '{"task": "過去", "due": "2026-07-01", "owners": []},'
               '{"task": "開催後", "due": "2026-09-10", "owners": []},'
               '{"task": "壊れ日付", "due": "8/8", "owners": []}]}')
        self.assertEqual(
            event_planner.parse_plan(raw, "2026-08-01", "2026-08-29"), [])

    def test_broken_json(self):
        self.assertEqual(
            event_planner.parse_plan("無理でした", "2026-08-01", "2026-08-29"),
            [])


class ProposalFlowTest(unittest.TestCase):
    EVENT = {"decision_id": 1, "name": "サマーカップは8/29に開催する",
             "event_date": "2026-08-29", "channel_id": 5,
             "source_message_id": 10}
    MS = [{"task": "グッズ発注", "due_date": "2026-08-08",
           "owners": "<@123>", "urgent": False}]

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_proposal_text(self):
        text = event_planner.build_proposal(self.EVENT, self.MS)
        self.assertIn("2026-08-29開催", text)
        self.assertIn("グッズ発注 → **2026-08-08**", text)
        self.assertIn("✅", text)

    def test_register_dedupes_by_decision(self):
        eid = event_planner.register_event(
            self.db_path, "agent1", self.EVENT, self.MS, "proposed")
        self.assertIsNotNone(eid)
        self.assertIsNone(event_planner.register_event(
            self.db_path, "agent1", self.EVENT, self.MS, "proposed"))
        # 検知クエリからも消えている（毎周期の再判定を防ぐ）
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1", decision="x", topic="",
                            source_kind="minutes", source_message_id=10,
                            channel_id=5, decided_on="t", created_at="t")
            rows = db.undetected_decisions_for_events(conn)
        self.assertNotIn(1, [r["id"] for r in rows])

    def test_approve_registers_action_items_once(self):
        eid = event_planner.register_event(
            self.db_path, "agent1", self.EVENT, self.MS, "proposed")
        with db.connect(self.db_path) as conn:
            db.set_event_proposal(conn, eid, 900)
        n = event_planner.approve(self.db_path, "agent1", 900)
        self.assertEqual(n, 1)
        with db.connect(self.db_path) as conn:
            items = db.open_action_items(conn, "agent1")
        self.assertEqual(items[0]["task"], "グッズ発注")
        self.assertEqual(items[0]["due_date"], "2026-08-08")
        self.assertEqual(items[0]["confirm_message_id"], 900)  # ❌一括取消可
        # 二重✅はCASで空振り
        self.assertIsNone(event_planner.approve(self.db_path, "agent1", 900))

    def test_dismiss(self):
        eid = event_planner.register_event(
            self.db_path, "agent1", self.EVENT, self.MS, "proposed")
        with db.connect(self.db_path) as conn:
            db.set_event_proposal(conn, eid, 900)
        self.assertTrue(event_planner.dismiss(self.db_path, 900))
        self.assertFalse(event_planner.dismiss(self.db_path, 900))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.open_action_items(conn, "agent1"), [])


if __name__ == "__main__":
    unittest.main()
