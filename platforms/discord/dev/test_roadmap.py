#!/usr/bin/env python3
"""進化ロードマップ（roadmap.py）のユニットテスト。

seedの冪等性・スコア順の選定・1枚ずつ運用・👍👎のCAS・起票連携を検証する。
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# insert(0)だと同名モジュール（bot.py等）がchatbot側に化ける

from core import db
from platforms.discord.dev import roadmap
class RoadmapTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _seed(self, items):
        with db.connect(self.db_path) as conn:
            for it in items:
                db.roadmap_seed_item(
                    conn, id=it["id"], title=it.get("title", f"案{it['id']}"),
                    description=it.get("desc", "説明"),
                    category=it.get("cat", "テスト"),
                    tier=it.get("tier", "quiet"),
                    route=it.get("route", "devbot"),
                    effect=it.get("effect", 3), cost=it.get("cost", 3),
                    created_at="2026-07-31T15:00:00")


class SeedTest(RoadmapTestBase):
    def test_real_seed_loads_100_and_is_idempotent(self):
        self.assertEqual(roadmap.seed(self.db_path), 100)
        self.assertEqual(roadmap.seed(self.db_path), 0)  # 再seedで重複しない
        with db.connect(self.db_path) as conn:
            counts = db.roadmap_counts(conn)
        self.assertEqual(counts, {"pending": 100})

    def test_reseed_keeps_progress(self):
        roadmap.seed(self.db_path)
        with db.connect(self.db_path) as conn:
            db.roadmap_mark_proposed(conn, 21, 999, "t")
        roadmap.seed(self.db_path)
        with db.connect(self.db_path) as conn:
            item = db.roadmap_by_message(conn, 999)
        self.assertEqual(item["id"], 21)  # status=proposedが残っている


class PickTest(RoadmapTestBase):
    def test_orders_by_score_then_id(self):
        # スコア= effect*2-cost: #2=7, #1=7, #3=8 → #3, #1, #2 の順
        self._seed([{"id": 1, "effect": 4, "cost": 1},
                    {"id": 2, "effect": 5, "cost": 3},
                    {"id": 3, "effect": 5, "cost": 2}])
        item = roadmap.pick_next(self.db_path)
        self.assertEqual(item["id"], 3)

    def test_only_one_card_at_a_time(self):
        self._seed([{"id": 1}, {"id": 2}])
        item = roadmap.pick_next(self.db_path)
        roadmap.mark_proposed(self.db_path, item["id"], 100)
        self.assertIsNone(roadmap.pick_next(self.db_path))  # 提案中は次を出さない

    def test_exhausted_returns_none(self):
        self.assertIsNone(roadmap.pick_next(self.db_path))


class DecideTest(RoadmapTestBase):
    def _propose(self, **kw):
        self._seed([dict({"id": 1}, **kw)])
        item = roadmap.pick_next(self.db_path)
        roadmap.mark_proposed(self.db_path, item["id"], 100)
        return item

    def test_approve_devbot_creates_cap_request(self):
        self._propose(route="devbot", title="宿題検出")
        d = roadmap.decide(self.db_path, 100, True, 100000000000000006)
        self.assertEqual(d["action"], "start_devbot")
        with db.connect(self.db_path) as conn:
            cap = db.get_capability_request(conn, d["cap_req_id"])
        self.assertIn("[RM#1] 宿題検出", cap["description"])
        self.assertEqual(cap["status"], "open")

    def test_approve_session_queues(self):
        self._propose(route="session")
        d = roadmap.decide(self.db_path, 100, True, 1)
        self.assertEqual(d["action"], "queued_session")
        self.assertIsNone(d["cap_req_id"])
        with db.connect(self.db_path) as conn:
            queue = db.roadmap_session_queue(conn)
        self.assertEqual([q["id"] for q in queue], [1])

    def test_reject_skips(self):
        self._propose()
        d = roadmap.decide(self.db_path, 100, False, 1)
        self.assertEqual(d["action"], "skipped")
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.roadmap_counts(conn), {"skipped": 1})

    def test_double_reaction_only_first_wins(self):
        self._propose()
        self.assertIsNotNone(roadmap.decide(self.db_path, 100, True, 1))
        self.assertIsNone(roadmap.decide(self.db_path, 100, False, 1))  # CAS負け

    def test_unknown_message_ignored(self):
        self._propose()
        self.assertIsNone(roadmap.decide(self.db_path, 555, True, 1))

    def test_decide_current_applies_to_proposed_card(self):
        # !roadmap要約への👍を「いま提案中のカード」への承認として扱う
        self._propose(route="devbot", title="宿題検出")
        d = roadmap.decide_current(self.db_path, True, 1)
        self.assertEqual(d["action"], "start_devbot")
        self.assertIsNotNone(d["cap_req_id"])
        # 決定済み後は対象なし（二重適用されない）
        self.assertIsNone(roadmap.decide_current(self.db_path, True, 1))

    def test_decide_current_without_proposal_is_none(self):
        self.assertIsNone(roadmap.decide_current(self.db_path, True, 1))

    def test_summary_and_current_decision_are_exclusive(self):
        # カード側とサマリー側の同時👍はCASで片方だけ勝つ
        self._propose()
        self.assertIsNotNone(roadmap.decide_current(self.db_path, False, 1))
        self.assertIsNone(roadmap.decide(self.db_path, 100, True, 1))


class CapWatchTest(RoadmapTestBase):
    """起票の自動拾い上げ（RM#21）: 検知・1件ずつ・CAS・👎で起票を閉じる。"""

    def _add_cap(self, agent_id="agent1", desc="動画編集がしたい"):
        with db.connect(self.db_path) as conn:
            return db.add_capability_request(
                conn, agent_id=agent_id, description=desc,
                context="会話", requested_by="1", source_msg_id=1,
                created_at="2026-07-31T18:00")

    def test_first_run_skips_existing_backlog(self):
        self._add_cap(desc="導入前の古い起票")
        self.assertIsNone(roadmap.watch_next_cap(self.db_path))  # 初期化のみ
        self.assertIsNone(roadmap.watch_next_cap(self.db_path))  # 古い分は出さない
        new_id = self._add_cap(desc="導入後の新しい起票")
        cap = roadmap.watch_next_cap(self.db_path)
        self.assertEqual(cap["id"], new_id)

    def test_roadmap_caps_are_excluded(self):
        roadmap.watch_next_cap(self.db_path)  # 初期化
        self._add_cap(agent_id="roadmap", desc="[RM#42] カード由来")
        self.assertIsNone(roadmap.watch_next_cap(self.db_path))

    def test_one_proposal_at_a_time(self):
        roadmap.watch_next_cap(self.db_path)
        cid1 = self._add_cap(desc="1件目")
        self._add_cap(desc="2件目")
        cap = roadmap.watch_next_cap(self.db_path)
        self.assertEqual(cap["id"], cid1)
        roadmap.mark_cap_proposed(self.db_path, cid1, 500)
        self.assertIsNone(roadmap.watch_next_cap(self.db_path))  # 👍👎待ち中

    def test_approve_keeps_cap_open_for_job(self):
        roadmap.watch_next_cap(self.db_path)
        cid = self._add_cap()
        roadmap.mark_cap_proposed(self.db_path, cid, 500)
        d = roadmap.decide_cap(self.db_path, 500, True)
        self.assertEqual(d, {"cap_id": cid, "approved": True})
        with db.connect(self.db_path) as conn:
            self.assertEqual(
                db.get_capability_request(conn, cid)["status"], "open")
        # 決定後は次の起票を提案できる
        cid2 = self._add_cap(desc="次")
        self.assertEqual(roadmap.watch_next_cap(self.db_path)["id"], cid2)

    def test_decline_closes_cap(self):
        roadmap.watch_next_cap(self.db_path)
        cid = self._add_cap()
        roadmap.mark_cap_proposed(self.db_path, cid, 500)
        d = roadmap.decide_cap(self.db_path, 500, False)
        self.assertFalse(d["approved"])
        with db.connect(self.db_path) as conn:
            self.assertEqual(
                db.get_capability_request(conn, cid)["status"], "rejected")

    def test_double_reaction_cas(self):
        roadmap.watch_next_cap(self.db_path)
        cid = self._add_cap()
        roadmap.mark_cap_proposed(self.db_path, cid, 500)
        self.assertIsNotNone(roadmap.decide_cap(self.db_path, 500, True))
        self.assertIsNone(roadmap.decide_cap(self.db_path, 500, False))

    def test_unknown_message_is_ignored(self):
        self.assertIsNone(roadmap.decide_cap(self.db_path, 999, True))

    def test_format_cap_proposal(self):
        text = roadmap.format_cap_proposal(
            {"id": 12, "agent_id": "agent1", "description": "動画編集がしたい"})
        self.assertIn("起票#12", text)
        self.assertIn("エージェント1", text)
        self.assertIn("👍", text)


class FormatTest(RoadmapTestBase):
    def test_card_contains_essentials(self):
        item = {"id": 42, "title": "朝のブリーフィング", "category": "先回り",
                "description": "毎朝1本に集約", "tier": "outward",
                "route": "devbot", "effect": 5, "cost": 2}
        text = roadmap.format_card(item)
        self.assertIn("#42", text)
        self.assertIn("朝のブリーフィング", text)
        self.assertIn("★★★★★", text)
        self.assertIn("👍", text)

    def test_progress_summary(self):
        self._seed([{"id": 1}, {"id": 2, "route": "session"}, {"id": 3}])
        item = roadmap.pick_next(self.db_path)
        roadmap.mark_proposed(self.db_path, item["id"], 100)
        text = roadmap.progress_summary(self.db_path)
        self.assertIn("判断済み 0/3", text)
        self.assertIn("いま提案中", text)

    def test_progress_summary_links_to_card(self):
        self._seed([{"id": 1}])
        item = roadmap.pick_next(self.db_path)
        roadmap.mark_proposed(self.db_path, item["id"], 100)
        text = roadmap.progress_summary(self.db_path, "9", "555")
        self.assertIn("discord.com/channels/9/555/100", text)
        self.assertIn("このメッセージに👍/👎", text)


if __name__ == "__main__":
    unittest.main()
