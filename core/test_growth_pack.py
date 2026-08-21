#!/usr/bin/env python3
"""自己成長パック（勝ちパターン学習＋自己採点の週次蒸留）のテスト。

./venv/bin/python -m unittest test_growth_pack -v
"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import proactive
from core import reminders
from core import selfreview_distill as svd


def _seed_spoke(conn, *, agent_id="agent1", message_id=100, kind="info",
                channel_id=5, content="良い感じの自発発言テキストっス"):
    """自発発言(spoke)＋投稿本文をDBへ植える。"""
    conn.execute(
        """INSERT INTO proactive_log(agent_id, kind, action, channel_id,
               posted_message_id, created_at)
           VALUES(?,?,'spoke',?,?,?)""",
        (agent_id, kind, channel_id, message_id, "2026-08-10T12:00"))
    conn.execute(
        """INSERT INTO messages(id, channel_id, author_id, content, created_at)
           VALUES(?,?,?,?,?)""",
        (message_id, channel_id, 1, content, "2026-08-10T12:00"))


def _add_feedback(conn, message_id, value, user_id="u1"):
    db.add_feedback(conn, message_id=message_id, agent_id="agent1",
                    kind="reaction", value=value, user_id=user_id,
                    created_at="2026-08-10T12:01")


class TestWinLessons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_up_on_spoke_records_win(self):
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        kind = proactive.record_win_from_feedback(self.tmp.name, 100)
        self.assertEqual(kind, "info")
        with db.connect(self.tmp.name) as conn:
            wins = db.recent_proactive_lessons(conn, "agent1", polarity="up")
        self.assertEqual(len(wins), 1)
        self.assertIn("良い感じ", wins[0]["text"])

    def test_up_on_normal_answer_is_ignored(self):
        """自発発言でない投稿への👍は勝ちパターンにしない（goldenの領分）。"""
        with db.connect(self.tmp.name) as conn:
            conn.execute(
                """INSERT INTO messages(id, channel_id, author_id, content,
                       created_at) VALUES(200, 5, 1, '普通の回答', 't')""")
        self.assertIsNone(
            proactive.record_win_from_feedback(self.tmp.name, 200))

    def test_lift_win_when_all_ups_removed(self):
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        proactive.record_win_from_feedback(self.tmp.name, 100)
        with db.connect(self.tmp.name) as conn:
            db.remove_feedback(conn, message_id=100, user_id="u1", value="up")
        self.assertTrue(proactive.lift_win_if_no_ups(self.tmp.name, 100))
        with db.connect(self.tmp.name) as conn:
            self.assertEqual(
                db.recent_proactive_lessons(conn, "agent1", polarity="up"), [])

    def test_win_does_not_leak_into_down_lessons(self):
        """極性の分離: 👍の勝ちパターンが👎教訓の取得に混ざらない。"""
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        proactive.record_win_from_feedback(self.tmp.name, 100)
        with db.connect(self.tmp.name) as conn:
            downs = db.recent_proactive_lessons(conn, "agent1")  # 既定=down
        self.assertEqual(downs, [])

    def test_lift_down_does_not_kill_win(self):
        """👎解除が👍由来の勝ちパターンを巻き添えにしない（極性つき解除）。"""
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        proactive.record_win_from_feedback(self.tmp.name, 100)
        # 👎ゼロの状態で解除判定が走っても（＝👎が付いて外れた後でも）
        proactive.lift_lesson_if_no_downs(self.tmp.name, 100)
        with db.connect(self.tmp.name) as conn:
            wins = db.recent_proactive_lessons(conn, "agent1", polarity="up")
        self.assertEqual(len(wins), 1)

    def test_build_wins_block(self):
        block = proactive.build_wins_block(
            [{"kind": "info", "text": "abc"}])
        self.assertIn("良い例", block)
        self.assertIn("abc", block)
        self.assertEqual(proactive.build_wins_block([]), "")


class TestSelfreviewDistill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _seed_scores(self, details):
        now = reminders.fmt(reminders.now_jst())
        with db.connect(self.tmp.name) as conn:
            for d in details:
                conn.execute(
                    """INSERT INTO proactive_log(agent_id, kind, action,
                           detail, created_at)
                       VALUES('agent1','selfreview','score',?,?)""",
                    (d, now))

    def test_parse_score_detail(self):
        self.assertEqual(svd.parse_score_detail("2|根拠のない断定"),
                         {"score": 2, "issue": "根拠のない断定"})
        self.assertIsNone(svd.parse_score_detail("junk"))
        self.assertIsNone(svd.parse_score_detail("9|範囲外"))
        self.assertIsNone(svd.parse_score_detail(None))

    def test_collect_only_low_scores(self):
        self._seed_scores(["2|曖昧", "5|", "1|断定", "4|長い", "3|冗長"])
        issues = svd.collect_low_issues(self.tmp.name, "agent1")
        self.assertEqual(sorted(issues),
                         [("selfreview", "断定"), ("selfreview", "曖昧")])

    def _seed_other(self, kind, action, detail):
        now = reminders.fmt(reminders.now_jst())
        with db.connect(self.tmp.name) as conn:
            conn.execute(
                """INSERT INTO proactive_log(agent_id, kind, action,
                       detail, created_at) VALUES('agent1',?,?,?,?)""",
                (kind, action, detail, now))

    def test_collect_spans_four_categories(self):
        """夜の自己監査が見せる4カテゴリ全部が蒸留の入力になる（2026-08-18）。"""
        self._seed_scores(["2|曖昧"])
        self._seed_other("fake_done", "assert_flagged", "納期は8/8で確定")
        self._seed_other("fake_done", "caught", "リマインダー登録の空約束")
        self._seed_other("recall", "silent", "懐疑役が差し止め: 根拠が薄い")
        issues = svd.collect_low_issues(self.tmp.name, "agent1")
        cats = sorted(c for c, _line in issues)
        self.assertEqual(cats, ["assertion", "fake_done", "selfreview",
                                "skeptic"])
        # 懐疑役は理由部分だけを渡す
        reason = next(ln for c, ln in issues if c == "skeptic")
        self.assertEqual(reason, "根拠が薄い")

    def test_prompt_groups_by_category(self):
        prompt = svd.build_prompt([("selfreview", "曖昧"),
                                   ("assertion", "納期を断定"),
                                   ("selfreview", "冗長")])
        self.assertIn("【低評価だった回答の問題点】", prompt)
        self.assertIn("【裏取りできないまま断定した箇所】", prompt)
        self.assertIn("- 曖昧", prompt)
        self.assertIn("- 冗長", prompt)
        self.assertIn("- 納期を断定", prompt)

    def test_high_score_excluded_but_others_kept(self):
        self._seed_scores(["5|問題なし"])
        self._seed_other("fake_done", "assert_flagged", "断定した")
        issues = svd.collect_low_issues(self.tmp.name, "agent1")
        self.assertEqual(issues, [("assertion", "断定した")])

    def test_distill_skips_when_samples_thin(self):
        self._seed_scores(["2|曖昧", "1|断定"])  # MIN_SAMPLES(3)未満
        called = []
        advice = svd.distill(self.tmp.name, "agent1", model="m",
                             invoke_fn=lambda p: called.append(p) or "[]")
        self.assertEqual(advice, [])
        self.assertEqual(called, [])  # LLMすら呼ばない

    def test_distill_stores_and_replaces(self):
        self._seed_scores(["2|曖昧", "1|断定", "2|冗長"])
        advice = svd.distill(
            self.tmp.name, "agent1", model="m",
            invoke_fn=lambda p: '["根拠を確認してから断定する", "結論から書く"]')
        self.assertEqual(len(advice), 2)
        # 2回目の蒸留で古い助言が差し替わる（積もらない）
        svd.distill(self.tmp.name, "agent1", model="m",
                    invoke_fn=lambda p: '["新しい助言"]')
        with db.connect(self.tmp.name) as conn:
            rows = db.recent_proactive_lessons(conn, "agent1", limit=10,
                                               polarity="advice")
        self.assertEqual([r["text"] for r in rows], ["新しい助言"])

    def test_parse_clamps_and_rejects(self):
        self.assertEqual(svd.parse("junk"), [])
        self.assertEqual(svd.parse('{"a":1}'), [])
        over = json.dumps([f"a{i}" for i in range(svd.MAX_ADVICE + 2)])
        self.assertEqual(len(svd.parse(over)), svd.MAX_ADVICE)
        long = svd.parse(f'["{"x" * 200}"]')
        self.assertEqual(len(long[0]), svd.MAX_ADVICE_LEN)

    def test_weekly_guard(self):
        mon9 = datetime(2026, 8, 17, 9, 30)  # 月曜9時台
        self.assertTrue(svd.should_run(self.tmp.name, "agent1", now=mon9))
        svd.mark_ran(self.tmp.name, "agent1", now=mon9)
        self.assertFalse(svd.should_run(self.tmp.name, "agent1", now=mon9))
        tue = datetime(2026, 8, 18, 9, 30)   # 火曜は曜日不一致
        self.assertFalse(svd.should_run(self.tmp.name, "agent1", now=tue))

    def test_advice_block(self):
        block = svd.build_advice_block([{"text": "結論から書く"}])
        self.assertIn("自己改善メモ", block)
        self.assertIn("結論から書く", block)
        self.assertEqual(svd.build_advice_block([]), "")


class TestMigration(unittest.TestCase):
    def test_polarity_added_to_existing_db(self):
        """polarity列の無い既存DBに冪等マイグレーションが効く。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            import sqlite3
            conn = sqlite3.connect(tmp.name)
            conn.execute(
                """CREATE TABLE proactive_lessons (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       agent_id TEXT, kind TEXT, channel_id INTEGER,
                       message_id INTEGER UNIQUE, text TEXT,
                       active INTEGER DEFAULT 1, created_at TEXT)""")
            conn.execute(
                """INSERT INTO proactive_lessons(agent_id, kind, text)
                   VALUES('agent1','handoff','既存の教訓')""")
            conn.commit()
            conn.close()
            db.init_db(tmp.name)  # 2回呼んでも冪等
            db.init_db(tmp.name)
            with db.connect(tmp.name) as c:
                rows = db.recent_proactive_lessons(c, "agent1")  # down扱い
            self.assertEqual([r["text"] for r in rows], ["既存の教訓"])
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()


class ThumbsDownDistillTest(unittest.TestCase):
    """通常回答への👎を蒸留に接続（2026-08-18）。30件が死蔵されていた。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)
        self.now = reminders.fmt(reminders.now_jst())
        with db.connect(self.tmp.name) as conn:
            db.upsert_channel(conn, id=7, name="g", type="text")
            db.upsert_user(conn, id=99, name="senko", display_name="AI戦子",
                           is_bot=True)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _answer(self, mid, content, value, proactive_post=False):
        with db.connect(self.tmp.name) as conn:
            db.insert_message(conn, id=mid, channel_id=7, author_id=99,
                              content=content, created_at=self.now)
            db.add_feedback(conn, message_id=mid, agent_id="senko",
                            kind="reaction", value=value, user_id="1",
                            created_at=self.now)
            if proactive_post:
                db.add_proactive_log(
                    conn, agent_id="senko", kind="recall", action="spoke",
                    channel_id=7, posted_message_id=mid,
                    created_at=self.now)

    def test_disliked_normal_answers_become_input(self):
        self._answer(1, "瓜生さんに直接聞くのが確実っス", "down")
        self._answer(2, "登録するっス（権限で失敗）", "down")
        self._answer(3, "褒められた回答", "up")            # 👍は入力にしない
        issues = svd.collect_low_issues(self.tmp.name, "senko")
        cats = [c for c, _ in issues]
        self.assertEqual(cats, ["thumbs_down", "thumbs_down"])
        texts = [t for _, t in issues]
        self.assertTrue(any("直接聞く" in t for t in texts))

    def test_proactive_posts_excluded(self):
        """自発発言への👎は既存の抑制学習の担当なので二重に使わない。"""
        self._answer(4, "自発発言だったもの", "down", proactive_post=True)
        self.assertEqual(svd.collect_low_issues(self.tmp.name, "senko"), [])

    def test_thumbs_down_block_comes_first(self):
        prompt = svd.build_prompt([("selfreview", "曖昧"),
                                   ("thumbs_down", "人に振っただけの回答")])
        i_down = prompt.index("👎を付けた回答")
        i_self = prompt.index("低評価だった回答")
        self.assertLess(i_down, i_self)   # 人間の👎を最初に読ませる
        self.assertIn("最重要", prompt)


class AbTestWiringTest(unittest.TestCase):
    """A/B実験の起動漏れ修正（2026-08-18）。変種ゼロで空回りしていた。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_pick_for_screen_seeds_itself(self):
        import ab_test
        with db.connect(self.tmp.name) as conn:
            self.assertEqual(db.active_variants(conn, ab_test.SLOT_SCREEN), [])
        vid, note = ab_test.pick_for_screen(self.tmp.name)   # 種撒き込み
        self.assertIsNotNone(vid)
        self.assertIsInstance(note, str)
        with db.connect(self.tmp.name) as conn:
            self.assertEqual(len(db.active_variants(conn,
                                                    ab_test.SLOT_SCREEN)), 2)

    def test_variant_note_reaches_screen_prompt(self):
        import proactive
        msgs = [{"id": 1, "channel": "g", "author": "常谷",
                 "content": "納期どうなってる"}]
        prompt = proactive.build_screen_prompt(
            msgs, "AI戦子", variant_note="- 一言添えるなら短くする")
        self.assertIn("一言添えるなら短くする", prompt)
        plain = proactive.build_screen_prompt(msgs, "AI戦子")
        self.assertNotIn("一言添えるなら短くする", plain)

    def test_attribution_roundtrip(self):
        import ab_test
        vid, _note = ab_test.pick_for_screen(self.tmp.name)
        detail = f"根拠あり [{ab_test.tag(vid)}]"
        self.assertEqual(ab_test.variant_from_detail(detail), vid)
        self.assertIsNone(ab_test.variant_from_detail("タグなし"))
        now = reminders.fmt(reminders.now_jst())
        with db.connect(self.tmp.name) as conn:
            db.add_proactive_log(conn, agent_id="senko", kind="recall",
                                 action="spoke", posted_message_id=555,
                                 detail=detail, created_at=now)
        got = ab_test.record_feedback_for_message(self.tmp.name, 555, "up")
        self.assertEqual(got, vid)
        with db.connect(self.tmp.name) as conn:
            row = next(v for v in db.active_variants(conn, ab_test.SLOT_SCREEN)
                       if v["id"] == vid)
        self.assertEqual(row["up"], 1)

    def test_untagged_message_ignored(self):
        import ab_test
        now = reminders.fmt(reminders.now_jst())
        with db.connect(self.tmp.name) as conn:
            db.add_proactive_log(conn, agent_id="senko", kind="recall",
                                 action="spoke", posted_message_id=556,
                                 detail="タグなし", created_at=now)
        self.assertIsNone(
            ab_test.record_feedback_for_message(self.tmp.name, 556, "up"))


class AdviceGraduationTest(unittest.TestCase):
    """助言のマージ保存と卒業（2026-08-18）。毎週学び直しを止める。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)
        self.now = reminders.fmt(reminders.now_jst())

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _merge(self, texts):
        with db.connect(self.tmp.name) as conn:
            return db.replace_advice_lessons(conn, "senko", texts, self.now)

    def test_same_text_increments_streak(self):
        self._merge(["断定しない"])
        merged = self._merge(["断定しない"])
        self.assertEqual(merged[0]["streak"], 2)
        merged = self._merge(["断定しない"])
        self.assertEqual(merged[0]["streak"], 3)
        with db.connect(self.tmp.name) as conn:
            rows = db.advice_lessons(conn, "senko")
        self.assertEqual(len(rows), 1)          # 重複行を作らない
        self.assertEqual(rows[0]["streak"], 3)

    def test_dropped_advice_deactivated(self):
        self._merge(["A", "B"])
        self._merge(["A"])                       # Bは今回出なかった
        with db.connect(self.tmp.name) as conn:
            texts = [r["text"] for r in db.advice_lessons(conn, "senko")]
        self.assertEqual(texts, ["A"])

    def test_cap_is_five(self):
        self.assertEqual(svd.MAX_ADVICE, 5)
        raw = '["1","2","3","4","5","6","7"]'
        self.assertEqual(len(svd.parse(raw)), 5)

    def test_prompt_asks_to_reuse_wording(self):
        prompt = svd.build_prompt([("selfreview", "曖昧")],
                                  previous=["断定しない"])
        self.assertIn("【前回までの助言】", prompt)
        self.assertIn("- 断定しない", prompt)
        self.assertIn("一字一句そのまま", prompt)
        plain = svd.build_prompt([("selfreview", "曖昧")])
        self.assertNotIn("前回までの助言", plain)

    def test_graduates_detected_at_threshold(self):
        def fake(_p):
            return '["断定しない"]'
        with db.connect(self.tmp.name) as conn:
            for d in ["2|a", "1|b", "2|c"]:
                conn.execute(
                    """INSERT INTO proactive_log(agent_id, kind, action,
                           detail, created_at)
                       VALUES('senko','selfreview','score',?,?)""",
                    (d, self.now))
        got = None
        for _ in range(svd.GRADUATE_STREAK):
            got = svd.distill_full(self.tmp.name, "senko", model="m",
                                   invoke_fn=fake)
        self.assertEqual(got["advice"][0]["streak"], svd.GRADUATE_STREAK)
        self.assertEqual(len(got["graduates"]), 1)
        post = svd.build_graduation_post(got["graduates"][0])
        self.assertIn("3週連続", post)
        self.assertIn("✅", post)

    def test_promote_creates_global_rule_and_frees_slot(self):
        merged = self._merge(["断定しない"])
        with db.connect(self.tmp.name) as conn:
            pid = db.add_advice_promotion(
                conn, agent_id="senko", lesson_id=merged[0]["id"],
                text="断定しない", streak=3, created_at=self.now)
            db.set_advice_promotion_message(conn, pid, 700)
            # 同じ助言の二重提案はしない
            self.assertIsNone(db.add_advice_promotion(
                conn, agent_id="senko", lesson_id=merged[0]["id"],
                text="断定しない", streak=3, created_at=self.now))
        text = svd.promote(self.tmp.name, 700, admin_id=1)
        self.assertEqual(text, "断定しない")
        with db.connect(self.tmp.name) as conn:
            rules_all = db.rules_all_active(conn)
            self.assertTrue(any(r["scope"] == "global"
                                and "断定しない" in r["rule_text"]
                                for r in rules_all))
            self.assertEqual(db.advice_lessons(conn, "senko"), [])  # 枠が空く
        self.assertIsNone(svd.promote(self.tmp.name, 700, admin_id=1))  # CAS

    def test_dismiss_keeps_advice(self):
        merged = self._merge(["断定しない"])
        with db.connect(self.tmp.name) as conn:
            pid = db.add_advice_promotion(
                conn, agent_id="senko", lesson_id=merged[0]["id"],
                text="断定しない", streak=3, created_at=self.now)
            db.set_advice_promotion_message(conn, pid, 701)
        self.assertTrue(svd.dismiss_promotion(self.tmp.name, 701))
        self.assertFalse(svd.dismiss_promotion(self.tmp.name, 701))
        with db.connect(self.tmp.name) as conn:
            self.assertEqual(len(db.advice_lessons(conn, "senko")), 1)

    def test_advice_block_shows_streak(self):
        block = svd.build_advice_block([{"text": "断定しない", "streak": 3},
                                        {"text": "新しい気づき", "streak": 1}])
        self.assertIn("断定しない（3週連続）", block)
        self.assertIn("新しい気づき", block)
        self.assertNotIn("新しい気づき（", block)
