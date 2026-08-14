#!/usr/bin/env python3
"""9件バッチ（RM#85/#90/#56/#41/#18/#40/#25/#23/#24）のユニットテスト。"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from core import auto_discover
from core import db
from core import event_planner
from core import honesty
from core import persona_review
from core import proactive
from core import pulse
from core import stale_watch


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        # WAL の残骸（-wal / -shm）も一緒に片付ける。消せなくても失敗にしない
        # （Windows は開いたままのファイルを消せないが、それは別のテストが検出する）
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class HallucinationTest(TestBase):
    """#85: 断定の自動裏取り。"""

    def test_verified_when_archive_supports(self):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=1, name="g", type="text")
            db.upsert_user(conn, id=1, name="u", display_name="u",
                           is_bot=False)
            db.insert_message(conn, id=10, channel_id=1, author_id=1,
                              content="サマーカップの納期は8月8日に決定",
                              created_at="t")
        ok, ex = honesty.verify_assertion(
            self.db_path, "サマーカップの納期は8月8日で確定しています")
        self.assertTrue(ok)
        self.assertIn("確定", ex)

    def test_unverified_without_evidence(self):
        ok, _ = honesty.verify_assertion(
            self.db_path, "ゼブラ計画の予算は500万円で確定しています")
        self.assertFalse(ok)

    def test_no_assertion_passes(self):
        ok, ex = honesty.verify_assertion(self.db_path, "たぶんそうかもです")
        self.assertTrue(ok)
        self.assertEqual(ex, "")


class SkepticTest(unittest.TestCase):
    """#90: 懐疑役の投稿前監査。"""

    CAND = {"kind": "recall"}

    def test_blocks_and_passes(self):
        ok, why = proactive.skeptic_check(
            "断定です", self.CAND, model="x",
            invoke_fn=lambda p: '{"post": false, "reason": "出典なし"}')
        self.assertFalse(ok)
        self.assertEqual(why, "出典なし")
        ok, _ = proactive.skeptic_check(
            "出典つき", self.CAND, model="x",
            invoke_fn=lambda p: '{"post": true, "reason": ""}')
        self.assertTrue(ok)

    def test_broken_output_passes(self):
        # 判定不能でゲート通過済みの発言を殺さない（安全方向の選択）
        ok, _ = proactive.skeptic_check(
            "本文", self.CAND, model="x", invoke_fn=lambda p: "??")
        self.assertTrue(ok)


class HandoffTest(unittest.TestCase):
    """#56: 引き継ぎの検出パース。"""

    def test_parse_handoff_validates(self):
        raw = ('{"candidates": [], "decisions": [], "handoff": ['
               '{"message_id": 10, "to": "agent2", "reason": "バナーの相談"},'
               '{"message_id": 11, "to": "unknown", "reason": "x"}]}')
        out = proactive.parse_screen_handoffs(raw, {10, 11},
                                              {"agent2", "agent3"})
        self.assertEqual(out, [{"message_id": 10, "to": "agent2",
                                "reason": "バナーの相談"}])

    def test_screen_returns_handoffs(self):
        msgs = [{"id": 10, "channel_id": 1, "channel": "g", "author_id": 1,
                 "author": "u", "content": "バナー作りたい", "created_at": "t"}]
        out = proactive.screen(
            msgs, agent_name="エージェント1",
            colleagues={"agent2": ("エージェント2", "デザイン")},
            invoke_fn=lambda p: '{"candidates": [], "decisions": [], '
                                '"handoff": [{"message_id": 10, '
                                '"to": "agent2", "reason": "専門領域"}]}')
        self.assertEqual(out["handoffs"][0]["to"], "agent2")


class StaleWatchTest(TestBase):
    """#41: 停滞プロジェクト検知。"""

    def _fill(self, conn, ch=7, n=20, days_ago_last=10):
        from datetime import timedelta
        now = datetime.utcnow()
        db.upsert_channel(conn, id=ch, name="proj", type="text")
        db.upsert_user(conn, id=1, name="u", display_name="u", is_bot=False)
        for i in range(n):
            created = (now - timedelta(days=days_ago_last + i % 20)) \
                .isoformat()
            db.insert_message(conn, id=ch * 1000 + i, channel_id=ch,
                              author_id=1, content=f"作業{i}",
                              created_at=created)

    def test_detects_stale_project_channel(self):
        with db.connect(self.db_path) as conn:
            self._fill(conn)
            db.add_action_item(conn, agent_id="agent1", source_message_id=1,
                               channel_id=7, task="残タスク", owners="<@1>",
                               due_date="2026-08-10", urgent=False,
                               created_at="t")
        ch = stale_watch.find_stale_channel(self.db_path)
        self.assertEqual(ch["channel_id"], 7)
        self.assertIn("止まってる", stale_watch.build_text(ch))

    def test_no_project_signal_is_ignored(self):
        with db.connect(self.db_path) as conn:
            self._fill(conn)   # 発言はあるが台帳の証拠なし
        self.assertIsNone(stale_watch.find_stale_channel(self.db_path))

    def test_active_channel_is_ignored(self):
        with db.connect(self.db_path) as conn:
            self._fill(conn, days_ago_last=0)   # 直近も動いている
            db.add_action_item(conn, agent_id="agent1", source_message_id=1,
                               channel_id=7, task="t", owners="<@1>",
                               due_date="2026-08-10", urgent=False,
                               created_at="t")
        self.assertIsNone(stale_watch.find_stale_channel(self.db_path))


class PulseTest(TestBase):
    """#18: 満足度パルス。"""

    def test_should_send_monthly_once(self):
        d1 = datetime(2026, 8, 1, 11, 30)
        self.assertTrue(pulse.should_send(self.db_path, "agent1", now=d1))
        pulse.mark_sent(self.db_path, "agent1", 900, now=d1)
        self.assertFalse(pulse.should_send(self.db_path, "agent1", now=d1))
        self.assertEqual(pulse.prev_message_id(self.db_path, "agent1"), 900)
        self.assertTrue(pulse.should_send(
            self.db_path, "agent1", now=datetime(2026, 9, 1, 11, 0)))

    def test_tally_subtracts_bot_seed_reactions(self):
        avg, votes = pulse.tally({"1️⃣": 1, "3️⃣": 2, "5️⃣": 3})
        # Bot自身の1票を引く → 3が1票・5が2票 = (3+10)/3
        self.assertEqual(votes, 3)
        self.assertAlmostEqual(avg, 13 / 3)
        self.assertEqual(pulse.tally({"1️⃣": 1}), (None, 0))

    def test_post_mentions_prev_result(self):
        text = pulse.build_post(4.2, 5, "8月")
        self.assertIn("平均⭐4.2（5票）", text)
        self.assertIn("1️⃣〜5️⃣", text)


class DesignLeadTest(unittest.TestCase):
    """#40: デザイン系節目の抽出。"""

    def test_design_milestones_filtered(self):
        row = {"milestones_json": json.dumps([
            {"task": "バナー入稿の締切", "due_date": "2026-08-15"},
            {"task": "会場の最終確認", "due_date": "2026-08-27"}],
            ensure_ascii=False)}
        out = event_planner.design_milestones(row)
        self.assertEqual(out, ["バナー入稿の締切 → 2026-08-15"])
        self.assertEqual(event_planner.design_milestones(
            {"milestones_json": "壊れ"}), [])


class PersonaReviewTest(TestBase):
    """#25/#23: 月次自己点検＋未使用プラグイン棚卸し。"""

    def test_collect_and_post(self):
        with db.connect(self.db_path) as conn:
            db.register_tool(conn, name="dice", marker="[DICE:",
                             source_req=1, created_at="t")
        proposals, unused = persona_review.review(
            self.db_path, ["agent1"], {"agent1": "エージェント1"}, model="x",
            invoke_fn=lambda p: "- アーカイブ担当: 語尾の絵文字を減らす提案")
        self.assertIn("絵文字", proposals)
        self.assertEqual(unused, ["dice"])   # 使用ログ無し→棚卸し対象
        post = persona_review.build_post(proposals, unused)
        self.assertIn("管理者判断", post)
        self.assertIn("dice", post)

    def test_monthly_gate(self):
        d = datetime(2026, 8, 1, 12, 10)
        self.assertTrue(persona_review.should_send(self.db_path, "agent1",
                                                   now=d))
        persona_review.mark_sent(self.db_path, "agent1", now=d)
        self.assertFalse(persona_review.should_send(self.db_path, "agent1",
                                                    now=d))


class AutoDiscoverTest(TestBase):
    """#24: 定型作業の自動化発見→👍起票。"""

    def test_too_few_tasks_skips_llm(self):
        def boom(p):
            raise AssertionError("材料不足で呼んだ")
        self.assertEqual(
            auto_discover.discover(self.db_path, model="x", invoke_fn=boom),
            [])

    def test_discover_register_approve_flow(self):
        with db.connect(self.db_path) as conn:
            for i in range(10):
                db.add_action_item(conn, agent_id="agent1",
                                   source_message_id=i, channel_id=1,
                                   task=f"毎週の集計作業{i}", owners="<@1>",
                                   due_date="2026-08-10", urgent=False,
                                   created_at="2026-07-25T10:00")
        ideas = auto_discover.discover(
            self.db_path, model="x",
            invoke_fn=lambda p: '{"ideas": [{"title": "集計の自動化", '
                                '"desc": "毎週の集計を自動投稿にする"}]}')
        self.assertEqual(len(ideas), 1)
        pid = auto_discover.register(self.db_path, ideas)
        auto_discover.set_message(self.db_path, pid, 900)
        text = auto_discover.build_post(ideas)
        self.assertIn("集計の自動化", text)
        ids = auto_discover.approve(self.db_path, "agent1", 900, 1)
        self.assertEqual(len(ids), 1)
        self.assertIsNone(auto_discover.approve(self.db_path, "agent1",
                                                900, 1))   # CAS
        with db.connect(self.db_path) as conn:
            cap = db.get_capability_request(conn, ids[0])
        self.assertIn("集計の自動化", cap["description"])
        self.assertEqual(cap["status"], "open")   # 開発BOTの自動拾い上げ対象


if __name__ == "__main__":
    unittest.main()
