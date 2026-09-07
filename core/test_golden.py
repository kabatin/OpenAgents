#!/usr/bin/env python3
"""ゴールデンセット（進化ロードマップ#16）のユニットテスト。

捕獲の入口の品質ゲート（2026-08-18）が中心。👍された投稿のうち
「一方的な発信」「相槌だけの質問」を評価データに混ぜないことを検証する。
"""

import os
import tempfile
import unittest

from core import db
from core import golden


class QualityGateTest(unittest.TestCase):
    """捕獲の入口の品質ゲート（2026-08-18）。23件中5件が壊れていた。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=7, name="g", type="text")
            db.upsert_user(conn, id=1, name="h", display_name="人B",
                           is_bot=False)
            db.upsert_user(conn, id=99, name="agent1", display_name="エージェント1",
                           is_bot=True)

    def tearDown(self):
        os.unlink(self.db_path)

    def _pair(self, q_id, a_id, question, answer, unsolicited=False):
        with db.connect(self.db_path) as conn:
            db.insert_message(conn, id=q_id, channel_id=7, author_id=1,
                              content=question, created_at="t")
            db.insert_message(conn, id=a_id, channel_id=7, author_id=99,
                              content=answer, created_at="t")
            if unsolicited:
                db.add_proactive_log(
                    conn, agent_id="agent1", kind="weekly", action="posted",
                    channel_id=7, posted_message_id=a_id, created_at="t")

    def test_praise_only_detection(self):
        for t in ["加点！", "おお、いいね", "偉い", "ありがとう", "さすが！",
                  "草", ""]:
            self.assertTrue(golden.is_praise_only(t), t)
        for t in ["リマインドリスト", "8/6にレプユニの相談をリマインドして"]:
            self.assertFalse(golden.is_praise_only(t), t)

    def test_unsolicited_post_not_captured(self):
        """週次レポートへの👍は質問が存在しないので入れない。"""
        self._pair(10, 11, "このスプシにシート追加が良さそうですね",
                   "📊 今週の自発活動レポート", unsolicited=True)
        self.assertFalse(golden.capture(self.db_path, "agent1", 11))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.count_golden(conn), 0)

    def test_praise_question_not_captured(self):
        self._pair(20, 21, "加点！", "リマインダーの複数宛先対応が入りました")
        self.assertFalse(golden.capture(self.db_path, "agent1", 21))

    def test_real_qa_still_captured(self):
        self._pair(30, 31, "リマインドリストを見せて",
                   "現在のリマインダーはこれです📋 id=42…")
        self.assertTrue(golden.capture(self.db_path, "agent1", 31))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.count_golden(conn, active_only=True), 1)

    def test_audit_disables_dirty_rows(self):
        """既存の汚れた行を仕分ける（第2段）。"""
        self._pair(40, 41, "正しい質問ですこれは長さも十分",
                   "正しい回答です")
        self._pair(50, 51, "こちらも十分な長さの質問です",
                   "📰 今週の業界ニュース", unsolicited=True)
        with db.connect(self.db_path) as conn:   # ゲート前の状態を再現
            for q, a in ((40, 41), (50, 51)):
                db.add_golden(conn, agent_id="agent1",
                              question=db.get_message(conn, q)["content"],
                              answer=db.get_message(conn, a)["content"],
                              source_answer_id=a, channel_id=7,
                              created_at="t")
            db.add_golden(conn, agent_id="agent1", question="加点！",
                          answer="どうもです", source_answer_id=999,
                          channel_id=7, created_at="t")
            self.assertEqual(db.count_golden(conn), 3)
        result = golden.audit_existing(self.db_path)
        self.assertEqual(result["kept"], 1)
        reasons = sorted(r for _i, r in result["disabled"])
        self.assertEqual(reasons, ["一方的投稿（質問なし）", "相槌・褒め言葉のみ"])
        # 二度目は何も落ちない（冪等）
        self.assertEqual(golden.audit_existing(self.db_path)["disabled"], [])


if __name__ == "__main__":
    unittest.main()


class AutoPostGateTest(unittest.TestCase):
    """proactive_logに載らない定時投稿も本文の書式で除外する（2026-08-18）。"""

    def test_auto_post_prefixes(self):
        for a in ["📊 今週の自発活動レポート（07/31〜）",
                  "⏰ <@1> リマインドです: 告知画像作成",
                  "🌙 今日の自己監査です",
                  "🗞 **ヤルキマン新聞**",
                  "🌊 新しい決定「…」で影響が出そうな記録があります"]:
            self.assertTrue(golden.is_auto_post(a), a)

    def test_normal_answers_pass(self):
        for a in ["了解です、8/6朝にリマインドをセットします📝",
                  "今わかる分だとこれです📋\n| id | 頻度 |",
                  "ID:105のurlに追加しますね。"]:
            self.assertFalse(golden.is_auto_post(a), a)


class GoldenCurateTest(unittest.TestCase):
    """キュレーション: 決定台帳・事実台帳・用語から候補を作り、人が採用する。
    👍自動捕獲（kind=auto）とは status/kind で区別し、旧来の呼び出しに混ざらない。"""

    def setUp(self):
        from core import golden_curate
        self.gc = golden_curate
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.add_decision(conn, agent_id="agent1", decision="定例は木曜19時",
                            topic="定例", source_kind="minutes",
                            source_message_id=1, channel_id=5,
                            decided_on="2026-08-01", created_at="2026-08-01T10:00")
            db.add_term(conn, term="技術責任者", description="開発全体の責任者",
                        created_by="1", created_at="2026-08-01T10:00")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sources_prompt_and_parse(self):
        with db.connect(self.path) as conn:
            src = self.gc.gather_sources(conn, "9")
        kinds = {s["kind"] for s in src}
        self.assertEqual(kinds, {"decision", "term"})
        self.assertIn("discord.com/channels/9/5/1", src[0]["link"])
        p = self.gc.build_prompt(src, 3, ["既存の質問"])
        self.assertIn("[決定#1]", p)
        self.assertIn("既存の質問", p)
        self.assertEqual(self.gc.parse("no json"), [])
        self.assertEqual(self.gc.parse('[1, "x"]'), [])
        items = self.gc.parse(
            '[{"question": "定例は？", "answer": "木曜です", '
            '"source": "決定#1", "link": "L"}]')
        self.assertEqual(items[0]["source"], "決定#1")

    def test_propose_saves_candidates_and_skips_duplicates(self):
        raw = ('[{"question": "定例は何曜日に決まった？", '
               '"answer": "木曜19時です。参照: L", "source": "決定#1", "link": "L"},'
               ' {"question": "定例は何曜日に決まったの？", "answer": "x", '
               '"source": "決定#1", "link": ""},'
               ' {"question": "技術責任者は誰？", "answer": "開発全体の責任者です", '
               '"source": "用語", "link": ""}]')
        saved = self.gc.propose(self.path, "agent1", n=3, invoke_fn=lambda p: raw)
        self.assertEqual(len(saved), 2)   # 言い直しの重複は1件に畳む
        with db.connect(self.path) as conn:
            rows = db.golden_rows(conn, statuses=("candidate",))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["kind"], "curated")
            self.assertEqual(rows[0]["note"], "決定#1")
            self.gc.set_status(self.path, [rows[0]["id"]], "curated")
            self.gc.set_status(self.path, [rows[1]["id"]], "rejected")
            self.assertEqual(len(db.golden_rows(conn, statuses=("curated",))), 1)
            # 旧来の active_only 呼び出しは候補を含めない
            self.assertEqual(db.golden_rows(conn), [])
            # 候補は👍捕獲の件数（active）にも混ざらない
            self.assertEqual(db.count_golden(conn, active_only=True), 0)

    def test_propose_skips_questions_already_known(self):
        with db.connect(self.path) as conn:
            db.add_golden_candidate(
                conn, agent_id="agent1", question="定例は何曜日に決まった？",
                answer="木曜19時です", source_link=None, note=None,
                created_at="2026-08-01T10:00")
        raw = ('[{"question": "定例は何曜日に決まったの？", "answer": "木曜です", '
               '"source": "決定#1", "link": ""}]')
        saved = self.gc.propose(self.path, "agent1", n=1, invoke_fn=lambda p: raw)
        self.assertEqual(saved, [])


class GoldenMigrationTest(unittest.TestCase):
    def test_legacy_table_gains_kind_columns(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db.init_db(path)
            with db.connect(path) as conn:
                conn.execute("DROP TABLE golden_set")
                conn.execute(
                    """CREATE TABLE golden_set (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT,
                        question TEXT, answer TEXT,
                        source_answer_id INTEGER UNIQUE, channel_id INTEGER,
                        created_at TEXT)""")
                conn.execute(
                    """INSERT INTO golden_set(agent_id, question, answer,
                           source_answer_id, channel_id, created_at)
                       VALUES('agent1', 'q', 'a', 10, 5, '2026-08-01T10:00')""")
            db.init_db(path)   # migration が走る（冪等）
            db.init_db(path)
            with db.connect(path) as conn:
                rows = db.golden_rows(conn)
            self.assertEqual(rows[0]["kind"], "auto")
            self.assertEqual(rows[0]["status"], "active")
            self.assertIsNone(rows[0]["note"])
        finally:
            os.unlink(path)
