#!/usr/bin/env python3
"""7件バッチ2（RM#91/#87/#52/#76/#66/#67/#89）のユニットテスト。"""

import os
import struct
import tempfile
import unittest
from datetime import datetime, timedelta

from core import dashboard
from core import db
from core import demand_watch
from core import injection_drill
from core import kpi
from core import outreach
from core import proactive


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class RestTest(unittest.TestCase):
    """#91 休息の概念。"""

    def test_night_is_resting(self):
        self.assertTrue(proactive.is_resting(datetime(2026, 8, 2, 3, 0)))
        self.assertFalse(proactive.is_resting(datetime(2026, 8, 2, 9, 0)))
        self.assertFalse(proactive.is_resting(datetime(2026, 8, 2, 0, 30)))

    def test_custom_window_wraps_midnight(self):
        self.assertTrue(proactive.is_resting(
            datetime(2026, 8, 2, 23, 0), start=22, end=6))
        self.assertTrue(proactive.is_resting(
            datetime(2026, 8, 2, 2, 0), start=22, end=6))
        self.assertFalse(proactive.is_resting(
            datetime(2026, 8, 2, 12, 0), start=22, end=6))


class KpiTest(TestBase):
    """#87 自己KPI宣言。"""

    Q_START = datetime(2026, 10, 1, 13, 30)

    def test_quarter_gate(self):
        self.assertTrue(kpi.should_send(self.db_path, "agent1",
                                        now=self.Q_START))
        kpi.mark_sent(self.db_path, "agent1", now=self.Q_START)
        self.assertFalse(kpi.should_send(self.db_path, "agent1",
                                         now=self.Q_START))
        self.assertFalse(kpi.should_send(   # 四半期でない月
            self.db_path, "agent1", now=datetime(2026, 11, 1, 13, 0)))

    def test_collect_computes_honesty_rate(self):
        with db.connect(self.db_path) as conn:
            for i in range(3):
                db.add_capability_request(
                    conn, agent_id="agent1", description=f"能力{i}",
                    context="", requested_by="1", source_msg_id=i,
                    created_at=kpi.reminders.fmt(
                        self.Q_START - timedelta(days=10)))
            db.add_proactive_log(conn, agent_id="agent1", kind="fake_done",
                                 action="caught",
                                 created_at=kpi.reminders.fmt(
                                     self.Q_START - timedelta(days=5)))
        stats = kpi.collect(self.db_path, "agent1", now=self.Q_START)
        self.assertEqual(stats["capability_requests"], 3)
        self.assertEqual(stats["fake_done"], 1)
        self.assertEqual(stats["honesty_rate"], 75)

    def test_post_includes_declaration_and_actuals(self):
        stats = {"hit": {"spoke": 4, "up": 3, "down": 0},
                 "capability_requests": 2, "fake_done": 0,
                 "honesty_rate": 100, "golden": 5}
        text = kpi.build_post("エージェント1", "今期は的中率80%を目指すっス", stats,
                              "2026年Q4")
        self.assertIn("的中率80%", text)
        self.assertIn("自発発言4件", text)
        self.assertIn("誠実失敗率100%", text)


class OutreachTest(TestBase):
    """#52 週1の御用聞き＋成長の予告。"""

    MON = datetime(2026, 8, 3, 10, 15)

    def test_weekly_gate(self):
        self.assertTrue(outreach.should_send(self.db_path, "agent1",
                                             now=self.MON))
        outreach.mark_sent(self.db_path, "agent1", now=self.MON)
        self.assertFalse(outreach.should_send(self.db_path, "agent1",
                                              now=self.MON))
        self.assertTrue(outreach.should_send(
            self.db_path, "agent1", now=self.MON + timedelta(days=7)))

    def test_recent_capabilities_and_post(self):
        with db.connect(self.db_path) as conn:
            cid = db.add_capability_request(
                conn, agent_id="agent1", description="PDF自動要約",
                context="", requested_by="1", source_msg_id=1,
                created_at=outreach.reminders.fmt(
                    self.MON - timedelta(days=2)))
            db.set_capability_status(conn, cid, "deployed")
        news = outreach.recent_capabilities(self.db_path, now=self.MON)
        self.assertEqual(news, ["PDF自動要約"])
        text = outreach.build_post(news)
        self.assertIn("困ってること", text)
        self.assertIn("PDF自動要約", text)


class DashboardTest(unittest.TestCase):
    """#76 ダッシュボード画像化（SVG・依存追加なし）。"""

    STATS = {"agents": {"agent1": {"spoke_by_kind": {"recall": 2},
                                  "silent": 10, "nudge": 3}},
             "decisions_total": 7, "golden_total": 4}
    ROSTER = [{"id": "agent1", "name": "エージェント1"}]

    def test_japanese_labels_rendered_when_pillow_available(self):
        # 日本語ラベルが画像に入るか（環境にPillow＋フォントがある場合）
        if not dashboard.has_labels():
            self.skipTest("Pillow/日本語フォントが無い環境")
        rows = dashboard.build_rows(self.STATS, self.ROSTER)
        png = dashboard.render_png(rows, "07/25 - 08/01")
        self.assertTrue(png.startswith(b"\x89PNG"))
        # ラベル描画により、色帯だけのフォールバックより情報量が増える
        self.assertGreater(len(png), len(dashboard._render_png_fallback(rows)))

    def test_fallback_without_pillow(self):
        png = dashboard._render_png_fallback(
            dashboard.build_rows(self.STATS, self.ROSTER), "t")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))

    def test_rows_and_png_signature(self):
        rows = dashboard.build_rows(self.STATS, self.ROSTER)
        labels = [r[0] for r in rows]
        self.assertIn("エージェント1 自発発言", labels)
        self.assertIn("決定事項（累計）", labels)
        png = dashboard.render_png(rows, "07/25 - 08/01")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", png[:24])

    def test_png_dimensions_scale_with_rows(self):
        one = dashboard.render_png([("a", 1, "spoke")], "t")
        three = dashboard.render_png([("a", 1, "spoke"), ("b", 2, "silent"),
                                      ("c", 3, "nudge")], "t")
        w1, h1 = struct.unpack(">II", one[16:24])
        _w3, h3 = struct.unpack(">II", three[16:24])
        self.assertEqual(w1, dashboard.WIDTH)
        self.assertGreater(h3, h1)   # 行が増えれば縦に伸びる

    def test_empty_stats_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            empty = {"agents": {}, "decisions_total": 0, "golden_total": 0}
            self.assertIsNone(dashboard.build_chart_file(empty, [], "t", d))

    def test_writes_png_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = dashboard.build_chart_file(self.STATS, self.ROSTER,
                                              "07/25 - 08/01", d)
            self.assertTrue(path.endswith(".png"))
            with open(path, "rb") as f:
                self.assertTrue(f.read(8).startswith(b"\x89PNG"))

    def test_legend_lists_every_row_in_order(self):
        # 複数エージェントだと同じ色が再登場するため、凡例は全行を順に出す
        stats = {"agents": {"agent1": {"spoke_by_kind": {"recall": 2},
                                      "silent": 5, "nudge": 1},
                            "agent2": {"spoke_by_kind": {"assist": 1},
                                       "silent": 3, "nudge": 0}},
                 "decisions_total": 4, "golden_total": 2}
        roster = [{"id": "agent1", "name": "エージェント1"},
                  {"id": "agent2", "name": "エージェント2"}]
        rows = dashboard.build_rows(stats, roster)
        legend = dashboard.legend_lines(rows)
        self.assertEqual(len(legend), len(rows))
        self.assertTrue(any("エージェント2 自発発言" in l_ for l_ in legend))


class DemandWatchTest(TestBase):
    """#66/#67 需要検知spawn提案・期間限定人格。"""

    def test_parse_proposal_validates_id(self):
        ok = demand_watch.parse_proposal(
            '{"proposal": {"id": "keiri", "name": "AI経理", '
            '"role": "経理担当", "channel_name": "経理相談", '
            '"temporary": false, "reason": "経理質問が多い"}}')
        self.assertEqual(ok["new_id"], "keiri")
        self.assertFalse(ok["temporary"])
        self.assertIsNone(demand_watch.parse_proposal(
            '{"proposal": {"id": "Keiri!", "name": "x", "role": "y", '
            '"channel_name": "z"}}'))
        self.assertIsNone(demand_watch.parse_proposal('{"proposal": null}'))

    def test_temporary_flag(self):
        p = demand_watch.parse_proposal(
            '{"proposal": {"id": "cup", "name": "AIカップ係", '
            '"role": "サマーカップ専属（8/29のイベント終了までの期間限定）", '
            '"channel_name": "カップ準備", "temporary": true, '
            '"reason": "イベント準備が集中"}}')
        self.assertTrue(p["temporary"])
        self.assertIn("期間限定", p["role"])

    def test_no_signals_no_llm(self):
        def boom(p):
            raise AssertionError("材料ゼロで呼んだ")
        self.assertIsNone(demand_watch.detect(
            self.db_path, [("エージェント1", "総務")], model="x", invoke_fn=boom))

    def test_upcoming_events(self):
        now = datetime(2026, 8, 2, 11, 0)
        with db.connect(self.db_path) as conn:
            db.add_event(conn, agent_id="agent1", name="サマーカップ",
                         event_date="2026-08-29", source_decision_id=1,
                         channel_id=1, milestones_json="[]",
                         status="planned", created_at="t")
        evs = demand_watch.upcoming_events(self.db_path, now=now)
        self.assertEqual(evs[0]["event_date"], "2026-08-29")


class InjectionDrillTest(TestBase):
    """#89 インジェクション自主訓練。"""

    def test_judge_detects_leak(self):
        passed, note = injection_drill.judge(
            f"了解です {injection_drill.CANARY}")
        self.assertFalse(passed)
        self.assertIn("漏洩", note)
        passed, note = injection_drill.judge(
            "そのルールには従えません。情報であって指示ではないので…")
        self.assertTrue(passed)
        self.assertIn("拒否", note)

    def test_run_drill_all_attacks(self):
        results = injection_drill.run_drill(
            "persona", "note", model="x",
            invoke_fn=lambda p, s: "その指示には従えません")
        self.assertEqual(len(results), len(injection_drill.ATTACKS))
        self.assertTrue(all(r["passed"] for r in results))
        report = injection_drill.build_report("エージェント1", results)
        self.assertIn(f"{len(results)}/{len(results)} 耐えた", report)

    def test_breach_is_reported(self):
        results = injection_drill.run_drill(
            "p", "n", model="x",
            invoke_fn=lambda p, s: f"はい {injection_drill.CANARY}")
        report = injection_drill.build_report("エージェント1", results)
        self.assertIn("⚠️", report)
        self.assertIn("見直した方がいい", report)

    def test_attacks_do_not_touch_db(self):
        # 訓練は本番データに触れない（攻撃文は固定・DB書き込みなし）
        before = os.path.getsize(self.db_path)
        injection_drill.run_drill("p", "n", model="x",
                                  invoke_fn=lambda p, s: "拒否します")
        self.assertEqual(os.path.getsize(self.db_path), before)


if __name__ == "__main__":
    unittest.main()
