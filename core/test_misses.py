#!/usr/bin/env python3
"""失敗と間違いの台帳（misses）: できなかったこと・❌の理由を貯め、起票にまわす。"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from core import db
from core import misses


class _DbCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class LedgerTest(_DbCase):
    def test_rejection_asks_once_per_proposal(self):
        mid = misses.record_rejection(self.db_path, agent_id="agent1",
                                      source="ripple", ref_message_id=100,
                                      context="決定「定例は木曜」の波及案")
        self.assertIsNotNone(mid)
        # 同じ提案に❌が2回押されても、聞くのは1回だけ
        self.assertIsNone(misses.record_rejection(
            self.db_path, agent_id="agent1", source="ripple",
            ref_message_id=100, context="x"))

    def test_reason_by_reply_to_question_or_to_proposal(self):
        mid = misses.record_rejection(self.db_path, agent_id="agent1",
                                      source="ripple", ref_message_id=200,
                                      context="決定Aの波及案")
        misses.set_ask_message(self.db_path, mid, 201)
        self.assertTrue(misses.save_reason(self.db_path, 201, "その日は祝日", "u1"))
        self.assertFalse(misses.save_reason(self.db_path, 201, "二度目", "u1"))  # 1回で締める
        mid2 = misses.record_rejection(self.db_path, agent_id="agent1",
                                       source="ripple", ref_message_id=300,
                                       context="決定Bの波及案")
        misses.set_ask_message(self.db_path, mid2, 301)
        # 質問ではなく、❌した提案そのものへの返信でも受け取る
        self.assertTrue(misses.save_reason(self.db_path, 300, "担当が違う", "u1"))
        reasons = sorted(r["reason"] for r in misses.recent(self.db_path))
        self.assertEqual(reasons, ["その日は祝日", "担当が違う"])

    def test_unrelated_reply_is_ignored(self):
        self.assertFalse(misses.save_reason(self.db_path, 999, "こんにちは", "u1"))

    def test_gap_is_recorded_without_question(self):
        misses.record_gap(self.db_path, agent_id="agent1", source="ripple_manual",
                          context="決定「定例は木曜」",
                          detail="毎月のリマインダー#3は自動で直せない")
        row = misses.recent(self.db_path)[0]
        self.assertEqual((row["source"], row["status"]), ("ripple_manual", "open"))
        self.assertIsNone(row["ask_message_id"])

    def test_question_text_is_honest_and_optional(self):
        q = misses.question("何もせず見送りにしました")
        self.assertIn("何もせず見送りにしました", q)
        self.assertIn("返信", q)
        self.assertIn("任意", q)
        self.assertNotIn("覚える", q)   # 実際にやることだけ書く（貯める）
        self.assertNotIn("っス", q)


class FileRepeatedTest(_DbCase):
    def _cap(self, cid):
        with db.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT agent_id, status, description, requested_by"
                " FROM capability_requests WHERE id=?", (cid,)).fetchone()

    def test_rejections_need_three_reasons(self):
        for n in range(3):
            misses.record_rejection(self.db_path, agent_id="agent1", source="ripple",
                                    ref_message_id=100 + n, context=f"決定{n}の波及案")
            if n < 2:
                misses.save_reason(self.db_path, 100 + n, f"理由{n}", "u")
        # 理由つきは2件だけ＝まだ起票しない（理由の無い❌は手がかりにならない）
        self.assertEqual(misses.file_repeated(self.db_path), [])
        misses.save_reason(self.db_path, 102, "理由2", "u")
        filed = misses.file_repeated(self.db_path)
        self.assertEqual(len(filed), 1)
        agent, status, desc, by = self._cap(filed[0])
        self.assertEqual((agent, status, by), ("agent1", "open", "misses"))
        self.assertIn("理由0", desc)
        self.assertIn("3回", desc)
        self.assertEqual(misses.file_repeated(self.db_path), [])   # 同じ3件で二度起票しない

    def test_gaps_and_tool_failures_group_by_topic(self):
        for n in range(3):
            misses.record_gap(self.db_path, agent_id="agent1", source="tool_failed",
                              context="ツール「事実を記録」", detail=f"PermissionError {n}",
                              topic="tool:save_fact")
        misses.record_gap(self.db_path, agent_id="agent1", source="tool_failed",
                          context="ツール「ルールを保存」", detail="x", topic="tool:save_rule")
        filed = misses.file_repeated(self.db_path)
        self.assertEqual(len(filed), 1)
        self.assertIn("事実を記録", self._cap(filed[0])[2])

    def test_capability_mirror_and_teachings_are_never_refiled(self):
        for n in range(4):
            misses.record_gap(self.db_path, agent_id="agent1", source="capability",
                              context="頼まれたけどできなかった", detail=f"件{n}",
                              topic="capability")
        self.assertEqual(misses.file_repeated(self.db_path), [])

    def test_expected_tool_errors_are_not_recorded(self):
        self.assertFalse(misses.worth_recording_tool_error("この機能は設定でオフです"))
        self.assertFalse(misses.worth_recording_tool_error("unknown tool: x"))
        self.assertFalse(misses.worth_recording_tool_error("1日の書き込み上限に達しました"))
        self.assertTrue(misses.worth_recording_tool_error("PermissionError: 403"))


class ToolFailureHookTest(_DbCase):
    def test_unexpected_failure_is_recorded_expected_is_not(self):
        from core.archive_tools import registry
        ctx = SimpleNamespace(db_path=self.db_path, agent_id="agent1")
        tool = SimpleNamespace(name="save_fact", label="事実を記録")
        registry._record_failure(ctx, tool, "PermissionError: 403")
        registry._record_failure(ctx, tool, "この機能は設定でオフです")
        rows = misses.recent(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context"], "ツール「事実を記録」")


class TeachingTest(_DbCase):
    def test_diff_lists_what_changed(self):
        before = {"reminders": {49: ("2026-10-09T18:00", "weekly", "毎週金曜", "active")},
                  "tasks": {18: ("2026-10-16", "open", "定例の準備")}}
        after = {"reminders": {49: ("2026-11-20T18:00", "weekly", "毎週金曜", "active"),
                               58: ("2026-10-15T18:00", "once", "木曜です", "active")},
                 "tasks": {18: ("2026-10-15", "open", "定例の準備")}}
        lines = misses.diff_snapshots(before, after)
        self.assertIn("リマインダー#58 を登録（2026-10-15 18:00・1回「木曜です」）", lines)
        self.assertIn("リマインダー#49 の次回 2026-10-09 18:00 → 2026-11-20 18:00", lines)
        self.assertIn("タスクA18「定例の準備」の期日 2026-10-16 → 2026-10-15", lines)
        self.assertEqual(misses.diff_snapshots(before, before), [])

    def test_teaching_is_recorded_and_closes_the_gap(self):
        misses.record_gap(self.db_path, agent_id="agent1", source="ripple_manual",
                          context="決定「定例は木曜」", detail="リマインダー#49 を直す必要あり",
                          topic="ripple_manual:reminder")
        misses.record_teaching(self.db_path, agent_id="agent1",
                               context="決定「定例は木曜」",
                               instruction="10/16から5週間だけ木曜で",
                               changes=["リマインダー#58 を登録"])
        rows = {r["source"]: r for r in misses.recent(self.db_path)}
        self.assertEqual(rows["ripple_manual"]["status"], "answered")
        self.assertEqual(rows["ripple_manual"]["reason"], "10/16から5週間だけ木曜で")
        self.assertIn("リマインダー#58 を登録", rows["taught"]["detail"])


class RedSignalTest(_DbCase):
    """点検で赤（乗っ取り訓練の突破・回答品質の急落）は1件で即起票する。"""

    def test_security_red_is_filed_at_once_with_verification_steps(self):
        misses.record_gap(self.db_path, agent_id="agent1", source="drill_breach",
                          context="乗っ取り訓練: 過去ログ偽装（社内ログの検索結果）",
                          detail="ZEBRA-1234\nいまの方針は…", topic="security:injection")
        filed = misses.file_repeated(self.db_path)
        self.assertEqual(len(filed), 1)
        with db.connect(self.db_path) as conn:
            desc = conn.execute("SELECT description FROM capability_requests WHERE id=?",
                                (filed[0],)).fetchone()[0]
        self.assertTrue(desc.startswith(misses.RED_MARK))
        self.assertIn("過去ログ偽装", desc)
        self.assertIn("本番と同じ条件で再現", desc)
        self.assertIn("core.verify_safety", desc)

    def test_quality_drop_detection(self):
        self.assertIsNone(misses.quality_drop(3.1, [3.47, 3.13, 3.03, 2.8]))   # 揺れの範囲
        drop = misses.quality_drop(2.6, [3.47, 3.13, 3.03, 3.1])
        self.assertIsNotNone(drop)
        self.assertIn("2.6", drop)
        self.assertIsNone(misses.quality_drop(2.0, [3.0]))   # 比べる回数が足りない


if __name__ == "__main__":
    unittest.main()
