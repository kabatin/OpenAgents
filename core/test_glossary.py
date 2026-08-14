#!/usr/bin/env python3
"""単語帳（glossary / RM#5）のユニットテスト。

マーカー抽出・決定論置換（長い語優先）・保存＋遡及修正・
議事録/検索向けの注入文を検証する。
"""

import os
import tempfile
import unittest

from core import db
from core import glossary


class MarkerTest(unittest.TestCase):
    def test_extract_add_and_cancel(self):
        text, adds, cancels, errors = glossary.extract_markers(
            "直しておきますね！\n[GLOSSARY: 夏季大会 | サマーカップ]\n"
            "[GLOSSARY_CANCEL: 旧語]")
        self.assertEqual(text, "直しておきますね！")
        self.assertEqual(adds, [("夏季大会", "サマーカップ")])
        self.assertEqual(cancels, ["旧語"])
        self.assertEqual(errors, [])

    def test_invalid_markers_become_errors(self):
        _, adds, _, errors = glossary.extract_markers(
            "[GLOSSARY: 同じ | 同じ]\n[GLOSSARY: " + "あ" * 60 + " | 正]")
        self.assertEqual(adds, [])
        self.assertEqual(len(errors), 2)

    def test_no_marker_passthrough(self):
        text, adds, cancels, errors = glossary.extract_markers("普通の返信")
        self.assertEqual((text, adds, cancels, errors),
                         ("普通の返信", [], [], []))


class ApplyTest(unittest.TestCase):
    def test_replaces_all_occurrences(self):
        out = glossary.apply("夏季大会の賞品は夏季大会用です",
                             [("夏季大会", "サマーカップ")])
        self.assertEqual(out, "サマーカップの賞品はサマーカップ用です")

    def test_longer_terms_replace_first(self):
        # 「夏季大会」が先に置換され「夏季」単体の置換に食われない
        pairs = [("夏季", "サマー"), ("夏季大会", "サマーカップ")]
        out = glossary.apply("夏季大会と夏季", pairs)
        self.assertEqual(out, "サマーカップとサマー")

    def test_empty_pairs_no_change(self):
        self.assertEqual(glossary.apply("そのまま", []), "そのまま")


class SaveTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_save_fixes_existing_ledgers(self):
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1",
                            decision="夏季大会の賞品はしゃもじに決定",
                            topic="夏季大会", source_kind="minutes",
                            source_message_id=1, channel_id=1,
                            decided_on="2026-07-24", created_at="t")
            db.add_action_item(conn, agent_id="agent1", source_message_id=1,
                               channel_id=1, task="夏季大会のグッズ発注",
                               owners="<@1>", due_date="2026-08-08",
                               urgent=False, created_at="t")
        fixed = glossary.save(self.db_path, "夏季大会", "サマーカップ",
                              "100000000000000006")
        self.assertEqual(fixed, 2)
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.glossary_pairs(conn),
                             [("夏季大会", "サマーカップ")])
            d = db.search_decisions(conn, ["サマー"])[0]
            self.assertIn("サマーカップ", d["decision"])
            self.assertEqual(d["topic"], "サマーカップ")
            item = db.open_action_items(conn, "agent1")[0]
            self.assertIn("サマーカップ", item["task"])

    def test_remove(self):
        glossary.save(self.db_path, "誤", "正", "1")
        self.assertTrue(glossary.remove(self.db_path, "誤"))
        self.assertFalse(glossary.remove(self.db_path, "誤"))
        self.assertEqual(glossary.load_pairs(self.db_path), [])


class NotesTest(unittest.TestCase):
    PAIRS = [("夏季大会", "サマーカップ")]

    def test_correction_table_for_minutes(self):
        table = glossary.build_correction_table(self.PAIRS)
        self.assertIn("「夏季大会」→「サマーカップ」", table)
        self.assertEqual(glossary.build_correction_table([]), "")

    def test_synonyms_note_for_search(self):
        note = glossary.synonyms_note(self.PAIRS)
        self.assertIn("サマーカップ=夏季大会", note)
        self.assertEqual(glossary.synonyms_note([]), "")

    def test_skill_note_teaches_marker(self):
        note = glossary.build_skill_note()
        self.assertIn("[GLOSSARY:", note)
        self.assertIn("正しい表記", note)



class TermTest(unittest.TestCase):
    """固有名詞辞書（ユーザー指定の正式表記登録）。"""

    def test_extract_term_with_and_without_desc(self):
        text, adds, cancels, errors = glossary.extract_term_markers(
            "登録しますね！\n[TERM: サマーカップ | 8/29開催の対抗戦イベント]\n"
            "[TERM: ウェイズビー]\n[TERM_CANCEL: 旧名称]")
        self.assertEqual(text, "登録しますね！")
        self.assertEqual(adds[0], {"term": "サマーカップ",
                                   "description": "8/29開催の対抗戦イベント"})
        self.assertEqual(adds[1], {"term": "ウェイズビー",
                                   "description": ""})
        self.assertEqual(cancels, ["旧名称"])
        self.assertEqual(errors, [])

    def test_terms_note_for_minutes(self):
        terms = [{"term": "サマーカップ", "description": "対抗戦イベント"}]
        note = glossary.build_terms_note(terms)
        self.assertIn("サマーカップ（対抗戦イベント）", note)
        self.assertIn("正式表記に直して", note)
        self.assertEqual(glossary.build_terms_note([]), "")

    def test_terms_context_for_answers(self):
        ctx = glossary.build_terms_context(
            [{"term": "サマーカップ", "description": "対抗戦イベント"}])
        self.assertIn("固有名詞辞書", ctx)
        self.assertIn("サマーカップ: 対抗戦イベント", ctx)

    def test_save_load_remove_term(self):
        import tempfile as _tf
        fd, path = _tf.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db.init_db(path)
            glossary.save_term(path, "サマーカップ", "対抗戦", "1")
            self.assertEqual(glossary.load_terms(path),
                             [{"term": "サマーカップ",
                               "description": "対抗戦"}])
            self.assertTrue(glossary.remove_term(path, "サマーカップ"))
            self.assertEqual(glossary.load_terms(path), [])
        finally:
            os.unlink(path)

    def test_skill_note_teaches_both_markers(self):
        note = glossary.build_skill_note()
        self.assertIn("[TERM:", note)
        self.assertIn("[GLOSSARY:", note)

if __name__ == "__main__":
    unittest.main()
