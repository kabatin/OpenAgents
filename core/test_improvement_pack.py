#!/usr/bin/env python3
"""改善パック（RM#55/#2/#13/#14/#16）のユニットテスト。"""

import os
import tempfile
import unittest

from core import db
from core import golden
from core import proactive
from core import rule_distill
from core import rules
from core import self_review


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class ApologyTest(unittest.TestCase):
    """#55 謝罪の作法: 訂正指示文に原因＋再発防止の作法が入っている。"""

    def test_correction_note_teaches_apology_form(self):
        note = rules.build_correction_note()
        self.assertIn("原因", note)
        self.assertIn("再発防止", note)
        self.assertIn("言い訳を長くしない", note)


class GoldenTest(TestBase):
    """#16 ゴールデンセット: 👍回答のQ&A自動保存。"""

    def _seed(self, conn):
        db.upsert_channel(conn, id=1, name="general", type="text")
        db.upsert_user(conn, id=1, name="u1", display_name="かば",
                       is_bot=False)
        db.upsert_user(conn, id=99, name="agent1", display_name="エージェント1",
                       is_bot=True)
        db.insert_message(conn, id=10, channel_id=1, author_id=1,
                          content="納期っていつでしたっけ？",
                          created_at="t")
        db.insert_message(conn, id=11, channel_id=1, author_id=99,
                          content="8/8です！", created_at="t")

    def test_capture_pairs_question_and_answer(self):
        with db.connect(self.db_path) as conn:
            self._seed(conn)
        self.assertTrue(golden.capture(self.db_path, "agent1", 11))
        self.assertFalse(golden.capture(self.db_path, "agent1", 11))  # 重複
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.count_golden(conn), 1)
            q = db.preceding_human_message(conn, 1, 11)
        self.assertIn("納期", q)

    def test_capture_without_question_is_skipped(self):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=1, name="g", type="text")
            db.upsert_user(conn, id=99, name="s", display_name="エージェント1",
                           is_bot=True)
            db.insert_message(conn, id=11, channel_id=1, author_id=99,
                              content="独り言", created_at="t")
        self.assertFalse(golden.capture(self.db_path, "agent1", 11))


class SelfReviewTest(unittest.TestCase):
    """#14 投稿セルフレビュー（シャドー採点）。"""

    def test_parse_and_bounds(self):
        self.assertEqual(
            self_review.parse('{"score": 4, "issue": "少し長い"}'),
            {"score": 4, "issue": "少し長い"})
        self.assertIsNone(self_review.parse('{"score": 9}'))
        self.assertIsNone(self_review.parse("採点不能"))

    def test_review_uses_invoke_fn(self):
        r = self_review.review("質問", "回答" * 100, model="x",
                               invoke_fn=lambda p: '{"score": 5, "issue": ""}')
        self.assertEqual(r["score"], 5)


class SilenceAuditTest(TestBase):
    """#13 沈黙の正解率検証。"""

    def test_audit_counts_missed(self):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=7, name="g", type="text")
            db.upsert_user(conn, id=1, name="u", display_name="かば",
                           is_bot=False)
            db.insert_message(conn, id=10, channel_id=7, author_id=1,
                              content="これ誰か分かります？", created_at="t")
            db.add_proactive_log(conn, agent_id="agent1", kind="recall",
                                 action="silent", channel_id=7,
                                 trigger_message_id=10,
                                 created_at="2026-08-01T10:00")
        def fake(prompt):
            self.assertIn("これ誰か分かります", prompt)
            import re, json as j
            lid = int(re.search(r"沈黙id=(\d+)", prompt).group(1))
            return j.dumps({"verdicts": [{"id": lid,
                                          "should_have_spoken": True}]})
        out = proactive.audit_silences(self.db_path, "agent1",
                                       "2026-07-25T00:00", model="x",
                                       invoke_fn=fake)
        self.assertEqual(out, {"total": 1, "missed": 1})

    def test_no_samples_returns_none(self):
        self.assertIsNone(proactive.audit_silences(
            self.db_path, "agent1", "2026-07-25T00:00", model="x",
            invoke_fn=lambda p: "{}"))


class RuleDistillTest(TestBase):
    """#2 週次ルール蒸留。"""

    def _add_rules(self, n):
        with db.connect(self.db_path) as conn:
            for i in range(n):
                db.add_rule(conn, agent_id="agent1", scope="global",
                            rule_text=f"ルール{i}", created_by="1",
                            source_msg_id=i, created_at="t")

    def test_too_few_rules_skips_llm(self):
        self._add_rules(3)
        def boom(p):
            raise AssertionError("ルールが少ないのに呼んだ")
        self.assertEqual(
            rule_distill.distill(self.db_path, model="x", invoke_fn=boom), [])

    def test_distill_and_approve_flow(self):
        self._add_rules(9)
        out = rule_distill.distill(
            self.db_path, model="x",
            invoke_fn=lambda p: '{"proposals": [{"rule_id": 1, '
                                '"reason": "id=2と重複"}]}')
        self.assertEqual(out[0]["rule_id"], 1)
        rid = rule_distill.register(self.db_path, out)
        rule_distill.set_message(self.db_path, rid, 500)
        text = rule_distill.build_proposal_text(out)
        self.assertIn("id=1", text)
        self.assertIn("✅", text)
        self.assertEqual(rule_distill.approve(self.db_path, 500), 1)
        self.assertIsNone(rule_distill.approve(self.db_path, 500))  # CAS
        with db.connect(self.db_path) as conn:
            active = db.rules_all_active(conn)
        self.assertEqual(len(active), 8)   # id=1が無効化された

    def test_unknown_rule_ids_dropped(self):
        self._add_rules(9)
        out = rule_distill.distill(
            self.db_path, model="x",
            invoke_fn=lambda p: '{"proposals": [{"rule_id": 999, '
                                '"reason": "x"}]}')
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
