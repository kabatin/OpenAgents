#!/usr/bin/env python3
"""記憶の取り込み口と測り方のテスト。

- 一次判定のついでに「いまの状況・訂正」を事実台帳に拾う（追加のLLM呼び出し無し）
- 主題の表記ゆれを既存の主題にそろえてから書く
- 一次判定が失敗した区間は次の周期に読み直す
- 回帰評価で「根拠の投稿を引けたか・引用したか」を数える
- 自己採点の採点役に、長めのツール結果とリプライ先を渡す
"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import facts
from core import golden_eval
from core import proactive
from core import self_review
from core.archive_tools import evidence

NOW = datetime(2026, 7, 31, 12, 0)


class _DbCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.path)

    def tearDown(self):
        os.unlink(self.path)


class TopicAlignTest(unittest.TestCase):
    """主題の表記ゆれ（add_fact の上書きが topic 完全一致なので寄せる）。"""

    def test_aligns_to_the_existing_wording(self):
        self.assertEqual(facts.align_topic("備品発注", ["備品の発注", "送料"]),
                         "備品の発注")
        self.assertEqual(facts.align_topic("ショップ送料", ["ショップの送料"]),
                         "ショップの送料")
        self.assertEqual(facts.align_topic("ショップの送料", ["ショップ送料"]),
                         "ショップ送料")

    def test_keeps_a_new_topic(self):
        self.assertEqual(facts.align_topic("別の主題", ["備品の発注"]), "別の主題")

    def test_noise_only_and_empty(self):
        self.assertEqual(facts.align_topic("", ["x"]), "")
        self.assertEqual(facts.align_topic("・・", ["x"]), "・・")

    def test_key_ignores_particles_and_marks(self):
        self.assertEqual(facts.topic_key("ショップの送料"),
                         facts.topic_key("ショップ送料"))
        self.assertNotEqual(facts.topic_key("送料"), facts.topic_key("在庫"))


class SaveExtractedTest(_DbCase):
    """会話から拾った事実の保存（台帳の書き込み口が回答中の save_fact だけだった）。"""

    def _save(self, items):
        return facts.save_extracted(self.path, "agent1", items,
                                    now="2026-11-02T10:00")

    def _active(self):
        with db.connect(self.path) as conn:
            return {r["topic"]: r["fact"]
                    for r in db.search_facts(conn, [], limit=50)}

    def test_saves_with_provenance(self):
        saved = self._save([{"topic": "ショップの送料", "fact": "国内送料は800円",
                             "message_id": 7, "channel_id": 5,
                             "stated_by": "ユーザー"}])
        self.assertEqual(len(saved), 1)
        with db.connect(self.path) as conn:
            row = db.search_facts(conn, ["送料"], limit=5)[0]
        self.assertEqual((row["source_message_id"], row["channel_id"],
                          row["stated_by"]), (7, 5, "ユーザー"))

    def test_same_topic_supersedes_even_with_different_wording(self):
        self._save([{"topic": "ショップの送料", "fact": "国内送料は900円"}])
        self._save([{"topic": "ショップ送料", "fact": "国内送料は800円に変更"}])
        active = self._active()
        self.assertEqual(list(active), ["ショップの送料"])
        self.assertIn("800円", active["ショップの送料"])

    def test_numeric_correction_is_not_dropped_as_a_rephrase(self):
        self._save([{"topic": "送料", "fact": "国内送料は900円"}])
        self.assertEqual(len(self._save([{"topic": "送料",
                                          "fact": "国内送料は800円"}])), 1)

    def test_same_content_is_not_rewritten(self):
        self._save([{"topic": "送料", "fact": "国内送料は800円"}])
        self.assertEqual(self._save([{"topic": "送料",
                                      "fact": "国内送料は800円"}]), [])
        with db.connect(self.path) as conn:
            self.assertEqual(len(db.search_facts(conn, [], limit=50)), 1)

    def test_skips_items_without_topic_or_fact(self):
        self.assertEqual(self._save([{"topic": "", "fact": "x"},
                                     {"topic": "y", "fact": "  "}]), [])


class ScreenFactsTest(unittest.TestCase):
    """一次判定のついでに拾う事実（追加のLLM呼び出し無し）。"""

    RAW = ('{"candidates": [], "decisions": [], "facts": ['
           '{"message_id": 1, "fact": "国内送料は800円に変更", "topic": "ショップの送料"},'
           '{"message_id": 9, "fact": "別chの話", "topic": "x"},'
           '{"message_id": 1, "fact": "主題なし"}], "handoff": []}')

    def test_extracts_only_valid_rows(self):
        got = proactive.parse_screen_facts(self.RAW, {1})
        self.assertEqual(got, [{"message_id": 1, "fact": "国内送料は800円に変更",
                                "topic": "ショップの送料"}])

    def test_caps_the_count(self):
        many = {"facts": [{"message_id": 1, "fact": f"f{i}", "topic": f"t{i}"}
                          for i in range(10)]}
        self.assertEqual(len(proactive.parse_screen_facts(json.dumps(many),
                                                          {1})),
                         proactive.MAX_FACTS)
        self.assertEqual(proactive.MAX_FACTS, 3)

    def test_broken_json_is_empty(self):
        self.assertEqual(proactive.parse_screen_facts("こわれた", {1}), [])
        self.assertEqual(proactive.parse_screen_facts("", {1}), [])
        self.assertEqual(proactive.parse_screen_facts(
            '{"facts": [{"message_id": "x", "fact": "a", "topic": "b"}]}',
            {1}), [])

    def test_prompt_asks_for_facts_and_screen_returns_them(self):
        msgs = [{"id": 1, "channel": "a", "author": "ユーザー",
                 "content": "送料は800円に変更"}]
        prompt = proactive.build_screen_prompt(msgs, "アシスタント")
        self.assertIn("facts に抽出", prompt)
        out = proactive.screen(msgs, agent_name="アシスタント",
                               invoke_fn=lambda p: self.RAW)
        self.assertEqual(out["facts"][0]["topic"], "ショップの送料")


class RewindCheckpointTest(_DbCase):
    """一次判定が失敗した区間を次の周期に読み直す（認証切れ中の取りこぼし対策）。"""

    def _msg(self, conn, mid, text="こんにちは"):
        db.upsert_channel(conn, id=1, name="general", type="text")
        db.upsert_user(conn, id=1, name="u1", display_name="ユーザー1")
        db.insert_message(conn, id=mid, channel_id=1, author_id=1,
                          content=text, created_at="2026-07-31T11:00:00")

    def _collect(self):
        return proactive.collect_cycle(self.path, "agent1", home_channel_id=100,
                                       exclude_channel_ids=(200,),
                                       daily_quota=3, now=NOW)

    def _state(self):
        with db.connect(self.path) as conn:
            return db.get_proactive_state(conn, "agent1")[
                "last_checked_message_id"]

    def test_failed_range_is_read_again(self):
        self._collect()                       # 初期化
        with db.connect(self.path) as conn:
            self._msg(conn, 10, text="送料は800円に変更")
            self._msg(conn, 11, text="在庫を登録済み")
        first = self._collect()
        self.assertEqual(first["checkpoint_from"], 0)
        self.assertEqual(self._state(), 11)   # 収集時に前進する
        self.assertTrue(proactive.rewind_checkpoint(
            self.path, "agent1", first["checkpoint_from"]))
        again = self._collect()               # 同じ区間がもう一度読める
        self.assertEqual([m["id"] for m in again["messages"]], [10, 11])

    def test_rewind_never_moves_forward_and_tolerates_missing_state(self):
        self._collect()
        with db.connect(self.path) as conn:
            self._msg(conn, 10)
        self._collect()
        self.assertFalse(proactive.rewind_checkpoint(self.path, "agent1", 99))
        self.assertEqual(self._state(), 10)
        self.assertFalse(proactive.rewind_checkpoint(self.path, "nobody", 0))


class EvidenceHitTest(unittest.TestCase):
    """根拠の投稿を引けたか（答え直しの実行ログから数える・追加のAI呼び出し無し）。"""

    ROW = {"source_link": "https://discord.com/channels/1/2/1540382550684471326"}

    def _events(self, text, as_list=False):
        content = [{"type": "text", "text": text}] if as_list else text
        return [{"type": "user", "message": {"content": [
            {"type": "tool_result", "content": content}]}}]

    def test_expected_id_from_source_link(self):
        self.assertEqual(golden_eval.expected_ids(self.ROW),
                         {"1540382550684471326"})
        self.assertEqual(golden_eval.expected_ids({}), set())

    def test_retrieved_when_a_tool_returned_the_source(self):
        ev = self._events("決定 … link: https://discord.com/channels/1/2/"
                          "1540382550684471326")
        hit = golden_eval.evidence_hit(self.ROW, ev, "答え")
        self.assertEqual(hit, {"retrieved": True, "cited": False})
        ev = self._events("…/1540382550684471326", as_list=True)
        self.assertTrue(golden_eval.evidence_hit(self.ROW, ev, "")["retrieved"])

    def test_not_retrieved_and_cited(self):
        ev = self._events("別の投稿 …/999999999999999999")
        hit = golden_eval.evidence_hit(
            self.ROW, ev,
            "参照: https://discord.com/channels/1/2/1540382550684471326")
        self.assertEqual(hit, {"retrieved": False, "cited": True})

    def test_no_source_or_no_events_is_not_judged(self):
        self.assertEqual(golden_eval.evidence_hit({}, [], "x"),
                         {"retrieved": None, "cited": None})
        self.assertIsNone(
            golden_eval.evidence_hit(self.ROW, None, "x")["retrieved"])

    def test_summary_divides_by_judged_only(self):
        items = [{"retrieved": True, "cited": True},
                 {"retrieved": False, "cited": False},
                 {"retrieved": None, "cited": None}]
        s = golden_eval.recall_summary(items)
        self.assertEqual(s["retrieved"], {"hits": 1, "n": 2, "rate": 0.5})
        self.assertEqual(golden_eval.recall_summary([])["cited"],
                         {"hits": 0, "n": 0, "rate": None})

    def test_evaluate_records_hits_and_summary(self):
        row = {"id": 1, "question": "送料は？", "answer": "800円です",
               **self.ROW}
        rep = golden_eval.evaluate(
            "unused.db", {"id": "agent1", "name": "a"}, [row],
            answer_fn=lambda r: ("800円です", {"events": self._events(
                "…/1540382550684471326")}),
            grade_fn=lambda r, a: '{"score": 5, "note": "ok"}',
            log=lambda *_: None, skills=frozenset())
        self.assertTrue(rep["items"][0]["retrieved"])
        self.assertEqual(rep["recall"]["retrieved"]["hits"], 1)

    def test_weekly_report_line(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "20261102-0900.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"mean": 4.1, "scored": 6, "recall": {
                    "retrieved": {"hits": 4, "n": 6}}}, f)
            g = proactive.golden_eval_latest(d)
        finally:
            for name in os.listdir(d):
                os.unlink(os.path.join(d, name))
            os.rmdir(d)
        self.assertEqual((g["retrieved"], g["retrieved_n"]), (4, 6))
        text = proactive.build_weekly_report({"golden_latest": g}, [], "11/1")
        self.assertIn("根拠の投稿を引けた: 4/6問", text)
        no_recall = dict(g, retrieved=None, retrieved_n=None)
        self.assertNotIn("根拠の投稿", proactive.build_weekly_report(
            {"golden_latest": no_recall}, [], "11/1"))


class ReviewInputsTest(unittest.TestCase):
    """採点役に渡す材料（足りずに正しい回答を減点していた）。"""

    def test_reply_context_reaches_the_reviewer(self):
        p = self_review.build_prompt("5桁のコードを出して", "12345です",
                                     context="[1] 認証コードのメール通知")
        self.assertIn("質問の文脈", p)
        self.assertIn("認証コードのメール通知", p)

    def test_long_evidence_is_kept(self):
        ev = "x" * 3000 + "id=50 次回 11/30"
        self.assertIn("id=50 次回 11/30",
                      self_review.build_prompt("q", "a", evidence=ev))

    def test_no_context_no_block(self):
        self.assertNotIn("質問の文脈", self_review.build_prompt("q", "a"))

    def test_review_passes_context(self):
        seen = []
        self_review.review("q", "a", model="m", context="リプライ先",
                           invoke_fn=lambda p: seen.append(p) or
                           '{"score": 5, "issue": ""}')
        self.assertIn("リプライ先", seen[0])

    def test_result_head_is_not_cut_at_120(self):
        body = "【リマインダー】" + "あ" * 150 + " id=50: 11/30(月) 18:00"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1",
                 "name": "mcp__archive__list_reminders", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": json.dumps(
                     {"ok": True, "hits": 1, "message": body},
                     ensure_ascii=False)}]}]}}]
        self.assertIn("id=50", evidence.summarize_for_review(events))


if __name__ == "__main__":
    unittest.main()
