#!/usr/bin/env python3
"""メタ・実験パック（RM#84/#86/#94/#61/#39/#93/#8/#46）のユニットテスト。"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import news_watch
from core import newspaper
from core import profiles
from core import prophecy
from core import search
from core import self_audit
from core import study_group


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class SelfAuditTest(TestBase):
    NOW = datetime(2026, 8, 2, 23, 10)

    def _log(self, kind, action, detail, when=None):
        with db.connect(self.db_path) as conn:
            db.add_proactive_log(
                conn, agent_id="agent1", kind=kind, action=action,
                detail=detail,
                created_at=(when or "2026-08-02T14:00"))

    def test_collects_only_suspicious_today(self):
        self._log("fake_done", "assert_flagged", "納期は8/8で確定")
        self._log("selfreview", "score", "2|根拠が薄い")
        self._log("selfreview", "score", "5|問題なし")      # 高評価は除外
        self._log("fake_done", "assert_flagged", "昨日の分",
                  when="2026-08-01T14:00")                   # 前日は除外
        items = self_audit.collect_audit(self.db_path, "agent1", now=self.NOW)
        self.assertEqual(len(items), 2)
        post = self_audit.build_audit_post(items)
        self.assertIn("自己監査", post)
        self.assertIn("裏取りできない断定", post)

    def test_quiet_day_posts_nothing(self):
        self.assertIsNone(self_audit.build_audit_post([]))

    def test_daily_gate(self):
        self.assertTrue(self_audit.should_audit(self.db_path, "agent1",
                                                now=self.NOW))
        self_audit.mark_audited(self.db_path, "agent1", now=self.NOW)
        self.assertFalse(self_audit.should_audit(self.db_path, "agent1",
                                                 now=self.NOW))


class BiasTest(TestBase):
    def _spoke(self, msg_id, author_id, ch, name):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=ch, name=name, type="text")
            db.upsert_user(conn, id=author_id, name=f"u{author_id}",
                           display_name=f"人{author_id}", is_bot=False)
            db.insert_message(conn, id=msg_id, channel_id=ch,
                              author_id=author_id, content="x",
                              created_at="t")
            db.add_proactive_log(conn, agent_id="agent1", kind="recall",
                                 action="spoke", channel_id=ch,
                                 trigger_message_id=msg_id,
                                 created_at="2026-08-01T10:00")

    def test_detects_concentration(self):
        for i in range(9):
            self._spoke(100 + i, 1, 7, "general")   # 全部同じ人・同じch
        stats = self_audit.collect_bias(self.db_path, "agent1",
                                        now=datetime(2026, 8, 2, 16, 0))
        result = self_audit.analyze_bias(stats)
        self.assertEqual(result["total"], 9)
        self.assertTrue(result["findings"])
        post = self_audit.build_bias_post("エージェント1", result)
        self.assertIn("⚠️", post)

    def test_small_sample_says_nothing(self):
        self._spoke(1, 1, 7, "g")
        stats = self_audit.collect_bias(self.db_path, "agent1",
                                        now=datetime(2026, 8, 2, 16, 0))
        self.assertIsNone(self_audit.analyze_bias(stats))
        self.assertIsNone(self_audit.build_bias_post("エージェント1", None))


class ProphecyTest(TestBase):
    NOW = datetime(2026, 9, 1, 17, 10)

    def _material(self, n=6):
        with db.connect(self.db_path) as conn:
            for i in range(n):
                db.add_decision(conn, agent_id="agent1",
                                decision=f"決定{i}", topic="t",
                                source_kind="minutes", source_message_id=i,
                                channel_id=1, decided_on="2026-08-20",
                                created_at="2026-08-20T10:00")

    def test_seal_and_open_cycle(self):
        self._material()
        sealed = prophecy.seal(
            self.db_path, "agent1", model="x", now=self.NOW,
            invoke_fn=lambda p: '{"predictions": [{"text": "発注が完了する", '
                                '"basis": "期日が今月"}]}')
        self.assertEqual(len(sealed), 1)
        self.assertFalse(prophecy.should_run(self.db_path, "agent1",
                                             now=self.NOW))   # 二重封印しない
        nxt = datetime(2026, 10, 1, 17, 5)
        opened = prophecy.open_envelope(
            self.db_path, "agent1", model="x", now=nxt,
            invoke_fn=lambda p: '{"verdicts": [{"index": 1, "hit": true, '
                                '"note": "完了記録あり"}]}')
        self.assertEqual(opened["period"], "2026-09")
        self.assertTrue(opened["verdicts"][0]["hit"])
        with db.connect(self.db_path) as conn:
            acc = db.prophecy_accuracy(conn, "agent1")
        self.assertEqual(acc, {"hit": 1, "total": 1})

    def test_no_material_no_seal(self):
        def boom(p):
            raise AssertionError("材料不足で呼んだ")
        self.assertEqual(prophecy.seal(self.db_path, "agent1", model="x",
                                       invoke_fn=boom, now=self.NOW), [])

    def test_post_shows_hits_and_accuracy(self):
        post = prophecy.build_post(
            [{"text": "来月の予測", "basis": "b"}],
            {"period": "2026-09", "preds": [{"text": "発注完了"}],
             "verdicts": [{"index": 1, "hit": False, "note": "未完"}]},
            {"hit": 3, "total": 5})
        self.assertIn("❌ 発注完了", post)
        self.assertIn("封印したっス", post)
        self.assertIn("通算的中率 60%", post)


class StudyGroupTest(TestBase):
    def _rules(self, n):
        with db.connect(self.db_path) as conn:
            for i in range(n):
                db.add_rule(conn, agent_id="agent2", scope=f"channel:{i}",
                            rule_text=f"ルール{i}", created_by="1",
                            source_msg_id=i, created_at="t")

    def test_few_rules_skips_llm(self):
        self._rules(2)
        def boom(p):
            raise AssertionError("ルール不足で呼んだ")
        self.assertEqual(study_group.find_shareable(
            self.db_path, model="x", invoke_fn=boom), [])

    def test_pick_and_promote_to_global(self):
        self._rules(5)
        picks = study_group.find_shareable(
            self.db_path, model="x",
            invoke_fn=lambda p: '{"picks": [{"rule_id": 1, '
                                '"reason": "全員共通"}]}')
        self.assertEqual(len(picks), 1)
        ids = study_group.register(self.db_path, picks, "agent1")
        study_group.set_message(self.db_path, ids, 700)
        self.assertIn("✅", study_group.build_post(picks))
        self.assertEqual(study_group.approve(self.db_path, 700), 1)
        with db.connect(self.db_path) as conn:
            scopes = {r["id"]: r["scope"] for r in db.rules_all_active(conn)}
        self.assertEqual(scopes[1], "global")
        self.assertIsNone(study_group.approve(self.db_path, 700))   # CAS

    def test_dismiss(self):
        self._rules(5)
        picks = study_group.find_shareable(
            self.db_path, model="x",
            invoke_fn=lambda p: '{"picks": [{"rule_id": 2, "reason": "r"}]}')
        ids = study_group.register(self.db_path, picks, "agent1")
        study_group.set_message(self.db_path, ids, 701)
        self.assertTrue(study_group.dismiss(self.db_path, 701))
        with db.connect(self.db_path) as conn:
            scopes = {r["id"]: r["scope"] for r in db.rules_all_active(conn)}
        self.assertNotEqual(scopes[2], "global")


class NewsWatchTest(TestBase):
    def test_parse_requires_url(self):
        items = news_watch.parse_items(
            '{"items": [{"title": "A社と提携", "why": "競合", '
            '"url": "https://example.com/1"},'
            '{"title": "URLなし", "why": "x"}]}')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "A社と提携")

    def test_caps_to_three(self):
        raw = json.dumps({"items": [
            {"title": f"n{i}", "why": "w", "url": f"https://e.com/{i}"}
            for i in range(6)]})
        self.assertEqual(len(news_watch.parse_items(raw)),
                         news_watch.MAX_ITEMS)

    def test_empty_post_is_none(self):
        self.assertIsNone(news_watch.build_post([]))
        post = news_watch.build_post(
            [{"title": "t", "why": "w", "url": "https://e.com"}])
        self.assertIn("業界ニュース", post)


class NewspaperTest(TestBase):
    NOW = datetime(2026, 8, 7, 20, 10)

    def _material(self, n=4):
        with db.connect(self.db_path) as conn:
            for i in range(n):
                db.add_decision(conn, agent_id="agent1", decision=f"決定{i}",
                                topic="t", source_kind="minutes",
                                source_message_id=i, channel_id=1,
                                decided_on="2026-08-05",
                                created_at="2026-08-05T10:00")

    def test_edit_and_image_prompt(self):
        self._material()
        paper = newspaper.edit(
            self.db_path, "08/07", model="x", now=self.NOW,
            invoke_fn=lambda p: '{"lead": "今週は発注が進んだ", "articles": '
                                '[{"headline": "グッズ発注が完了", '
                                '"deck": "原価45万円に圧縮", '
                                '"body": "グッズ発注が完了した。納品は月末。", '
                                '"art": "段ボールのイラスト"}, '
                                '{"headline": "カップ準備が本格化", '
                                '"deck": "", "body": "", "art": ""}]}')
        self.assertEqual(len(paper["articles"]), 2)
        prompt = newspaper.build_image_prompt(paper, "08/07", issue_no=3)
        self.assertIn("社内新聞", prompt)
        self.assertIn("【トップ記事】", prompt)
        self.assertIn("グッズ発注が完了", prompt)
        self.assertIn("原価45万円に圧縮", prompt)
        self.assertIn("段ボールのイラスト", prompt)
        self.assertIn("第3号", prompt)
        self.assertIn("タブロイド", prompt)
        self.assertIn("誤字なく正確", prompt)   # 文字崩れ対策の指示
        caption = newspaper.build_caption(paper, "08/07", issue_no=3)
        self.assertIn("今週は発注が進んだ", caption)
        self.assertIn("**■ グッズ発注が完了** — 原価45万円に圧縮", caption)
        self.assertIn("納品は月末", caption)

    def test_old_format_fallback(self):
        """旧形式（headlines配列）のJSONでも紙面が組める。"""
        paper = newspaper.parse_headlines(
            '{"lead": "l", "headlines": ["見出しA"]}')
        self.assertEqual(paper["articles"][0]["headline"], "見出しA")
        self.assertIn("見出しA", newspaper.build_image_prompt(paper, "08/07"))
        self.assertIn("**■ 見出しA**", newspaper.build_caption(paper, "08/07"))

    def test_no_material_means_no_paper(self):
        def boom(p):
            raise AssertionError("材料不足で呼んだ")
        self.assertIsNone(newspaper.edit(self.db_path, "08/07", model="x",
                                         invoke_fn=boom, now=self.NOW))

    def test_weekly_gate(self):
        self.assertTrue(newspaper.should_publish(self.db_path, "agent1",
                                                 now=self.NOW))
        newspaper.mark_published(self.db_path, "agent1", now=self.NOW)
        self.assertFalse(newspaper.should_publish(self.db_path, "agent1",
                                                  now=self.NOW))


class StyleAndToneTest(unittest.TestCase):
    """#8 会話スタイル学習・#46 感情トーン検知（プロンプト規約）。"""

    def test_profile_prompt_learns_style(self):
        prompt = profiles.build_update_prompt("佐藤", None, [
            {"channel": "g", "content": "了解"}])
        self.assertIn("会話スタイル", prompt)
        self.assertIn("絵文字", prompt)

    def test_profile_block_tells_to_match_style(self):
        block = profiles.build_profile_block(
            {"display_name": "佐藤", "profile": "- 簡潔派"})
        self.assertIn("寄せる", block)
        self.assertIn("内容は変えない", block)

    def test_tone_rule_forbids_mentioning_emotion(self):
        tmpl = search.ANSWER_SYSTEM_TMPL
        self.assertIn("トーンの調整", tmpl)
        self.assertIn("感情そのものには言及しない", tmpl)


if __name__ == "__main__":
    unittest.main()


class SelfAuditLogTest(TestBase):
    """夜の自己監査の内訳ログ（2026-08-18）。報告だけで終わらせない。"""

    def test_category_counts_and_format(self):
        items = [{"kind": "fake_done", "action": "assert_flagged",
                  "detail": "a"},
                 {"kind": "fake_done", "action": "assert_flagged",
                  "detail": "b"},
                 {"kind": "selfreview", "action": "score", "detail": "2|c"}]
        counts = self_audit.category_counts(items)
        self.assertEqual(counts, {"assert_flagged": 2, "score": 1})
        text = self_audit.format_counts(counts)
        self.assertIn("裏取りできない断定2件", text)
        self.assertIn("セルフレビュー低評価1件", text)

    def test_empty_counts(self):
        self.assertEqual(self_audit.format_counts({}), "0件")
