#!/usr/bin/env python3
"""事実台帳＋訂正検知の緩和＋できたフリ拡張（2026-08-18）のテスト。

背景の実事故: グッズ納期の訂正で戦子が「認識更新するっス」と答えたが、
DBには何も残らなかった（受け皿が無い・訂正検知が発火しない・
できたフリ検出の網外、の三重の穴）。
"""

import os
import tempfile
import unittest

from core import db
from core import facts
from core import honesty
from core import rules


class ParseTest(unittest.TestCase):
    def test_parse_and_extract(self):
        text, adds, cancels, errors = facts.extract_markers(
            "了解っス\n[FACT: 8/8グッズ販売 | 販売は完了済み。9月到着分は"
            "オンラインで販売予定]\n[FACT_CANCEL: 7]")
        self.assertEqual(text, "了解っス")
        self.assertEqual(adds[0]["topic"], "8/8グッズ販売")
        self.assertIn("完了済み", adds[0]["fact"])
        self.assertEqual(cancels, [7])
        self.assertEqual(errors, [])

    def test_markers_always_removed_even_on_error(self):
        text, adds, _c, errors = facts.extract_markers(
            "本文[FACT: 主題だけ]")
        self.assertEqual(text, "本文")     # 生マーカーを晒さない
        self.assertEqual(adds, [])
        self.assertTrue(errors)

    def test_too_long_rejected(self):
        with self.assertRaises(ValueError):
            facts.parse_fact("主題 | " + "あ" * (facts.MAX_FACT_LEN + 1))
        with self.assertRaises(ValueError):
            facts.parse_fact("あ" * (facts.MAX_TOPIC_LEN + 1) + " | 事実")

    def test_cap_per_answer(self):
        payload = "".join(f"[FACT: 主題{i} | 事実{i}]" for i in range(5))
        _t, adds, _c, errors = facts.extract_markers(payload)
        self.assertEqual(len(adds), facts.MAX_PER_ANSWER)
        self.assertTrue(errors)

    def test_pipe_in_fact_kept(self):
        got = facts.parse_fact("納期 | A|B の二系統")
        self.assertEqual(got["fact"], "A|B の二系統")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _add(self, topic, fact, mid=None):
        with db.connect(self.db_path) as conn:
            return db.add_fact(
                conn, agent_id="senko", topic=topic, fact=fact,
                source_kind="conversation", source_message_id=mid,
                channel_id=7 if mid else None, stated_by="樺山",
                created_at="2026-08-18T05:52")

    def test_same_topic_supersedes(self):
        self._add("8/8グッズ販売", "9月4〜6日到着予定で販売は未実施")
        fid, superseded = self._add("8/8グッズ販売", "販売は完了済み")
        self.assertEqual(superseded, 1)      # 古い認識が上書きされる
        with db.connect(self.db_path) as conn:
            rows = db.search_facts(conn, ["グッズ"])
        self.assertEqual(len(rows), 1)       # activeは最新だけ
        self.assertEqual(rows[0]["id"], fid)
        self.assertIn("完了済み", rows[0]["fact"])

    def test_other_topic_untouched(self):
        self._add("納期", "9月到着")
        _fid, superseded = self._add("価格", "タオル3000円")
        self.assertEqual(superseded, 0)
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.count_facts(conn), 2)

    def test_ledger_block_has_source_link(self):
        self._add("8/8グッズ販売", "販売は完了済み", mid=1539149855996256338)
        block = facts.build_ledger_block(self.db_path, ["グッズ"], "999")
        self.assertIn("事実台帳", block)
        self.assertIn("[8/8グッズ販売] 販売は完了済み", block)
        self.assertIn("樺山さん談", block)
        self.assertIn("discord.com/channels/999/7/", block)

    def test_empty_ledger(self):
        self.assertEqual(facts.build_ledger_block(self.db_path, ["無い"], "1"),
                         "")

    def test_cancel(self):
        fid, _ = self._add("納期", "9月到着")
        with db.connect(self.db_path) as conn:
            self.assertTrue(db.cancel_fact(conn, fid))
            self.assertFalse(db.cancel_fact(conn, fid))   # 二重取消は不可
            self.assertEqual(db.count_facts(conn), 0)

    def test_skill_note_lists_and_distinguishes(self):
        self._add("納期", "9月到着")
        with db.connect(self.db_path) as conn:
            recent = db.search_facts(conn, [], limit=10)
        note = facts.build_skill_note(recent)
        self.assertIn("[FACT: 主題 | 事実の内容]", note)
        self.assertIn("[納期] 9月到着", note)
        self.assertIn("[RULE:", note)      # 使い分けの明示
        self.assertIn("[TERM:", note)


class CorrectionDetectionTest(unittest.TestCase):
    """訂正語なしの事実申告を拾えるか（実事故の再現）。"""

    ACTUAL = ("8月8日のグッズ販売自体は滞りなく完了していて、"
              "9月到着の納品物についてはiXAオンラインショップや"
              "今後のオフラインイベント等で販売する予定のものです")

    def test_actual_incident_message_now_detected(self):
        self.assertFalse(rules.looks_like_correction(self.ACTUAL))  # 従来は不発
        self.assertTrue(rules.looks_like_state_update(self.ACTUAL))

    def test_question_not_treated_as_statement(self):
        self.assertFalse(rules.looks_like_state_update("販売は完了しましたか？"))
        self.assertFalse(rules.looks_like_state_update("もう終わってる?"))

    def test_plain_chat_ignored(self):
        self.assertFalse(rules.looks_like_state_update("おはようございます"))
        self.assertFalse(rules.looks_like_state_update(""))

    def test_correction_words_still_work(self):
        self.assertTrue(rules.looks_like_correction("それ違うよ、正しくは19時"))

    def test_note_demands_marker_not_promise(self):
        note = rules.build_correction_note()
        self.assertIn("[FACT:", note)
        self.assertIn("口約束", note)       # 「認識を更新します」だけは禁止
        state_note = rules.build_correction_note(state_only=True)
        self.assertIn("状況の反映", state_note)
        self.assertIn("訂正とは限らない", state_note)


class FakeDoneTest(unittest.TestCase):
    """「認識更新するっス」の空約束を検出できるか。"""

    def test_memory_claim_without_deed_is_caught(self):
        answer = ("了解っス、8/8グッズ販売完了、9月到着分はオンライン向け"
                  "在庫という理解で認識更新するっス。")
        self.assertEqual(honesty.detect_fake_done(answer), ["memory"])
        note = honesty.build_fake_done_note(["memory"])
        self.assertIn("認識の更新", note)

    def test_memory_claim_with_fact_marker_is_ok(self):
        answer = ("了解っス、認識更新するっス。\n"
                  "-# 🧠 事実を記録(id=1): [8/8グッズ販売] 販売は完了済み")
        self.assertEqual(honesty.detect_fake_done(answer), [])

    def test_rule_deed_also_satisfies_memory_claim(self):
        answer = ("覚えておくっス。\n"
                  "-# 📌 ルール登録(id=3, このチャンネル): 訂正: 定例は19時")
        self.assertEqual(honesty.detect_fake_done(answer), [])

    def test_plain_answer_not_flagged(self):
        self.assertEqual(honesty.detect_fake_done("8/8の販売は完了っスね"), [])

    def test_existing_claims_still_detected(self):
        self.assertEqual(
            honesty.detect_fake_done("リマインダー登録しました"), ["remind"])


if __name__ == "__main__":
    unittest.main()
