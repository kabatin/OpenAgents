#!/usr/bin/env python3
"""v4 ツールループの配線（core 側）: runner_answer の素通しと注入の切り替え、
自己採点への根拠注入、観察ループ（二次判定）へのツール注記、golden_eval。"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import db
from core import golden_eval
from core import invoke_claude
from core import proactive
from core import runner_answer
from core import search
from core import self_review
from core.archive_tools import evidence


def _tool_use(tid, name, inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _tool_result(tid, payload):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": json.dumps(payload)}]}]}}


class _TmpDb(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class RunnerAnswerPassthroughTest(_TmpDb):
    def _run(self, **kw):
        captured = {}

        class R:
            text = "答え"
            session_id = None
            events = [{"type": "result"}]
            meta = {"cost_usd": 0.1}

        def fake_invoke(prompt, **kwargs):
            captured.update(kwargs)
            return R()

        with patch.object(invoke_claude, "invoke", fake_invoke), \
                patch.object(search, "extract_keywords",
                             return_value=["kw"]), \
                patch.object(search, "search_messages", return_value=[]):
            res = runner_answer.answer_question(
                self.db_path, "1", "質問", agent=search.DEFAULT_AGENT, **kw)
        return captured, res

    def test_mcp_kwargs_and_events_returned(self):
        cap, res = self._run(mcp_config='{"x":1}',
                             mcp_allow=("mcp__archive__get_facts",),
                             max_budget_usd=0.5, on_event=print)
        self.assertEqual(cap["mcp_config"], '{"x":1}')
        self.assertIn("mcp__archive__get_facts", cap["allow"])
        self.assertIn("WebSearch", cap["allow"])
        self.assertEqual(cap["max_budget_usd"], 0.5)
        self.assertIs(cap["on_event"], print)
        self.assertEqual(res["events"], [{"type": "result"}])
        self.assertEqual(res["meta"]["cost_usd"], 0.1)
        self.assertIn("prompt", res)
        self.assertIn("system", res)

    def test_without_mcp_nothing_added(self):
        cap, res = self._run()
        self.assertNotIn("mcp_config", cap)
        self.assertNotIn("on_event", cap)
        self.assertEqual(res["answer"], "答え")
        # 旧来の呼び出し元は events/meta を知らない（戻り値の契約を変えない）
        self.assertNotIn("events", res)


class StepDInjectionTest(_TmpDb):
    """Step D: 注入の切り替え・v4 system・キーワード抽出の省略。"""

    def _run(self, **kw):
        calls = {"extract": 0, "search": [], "invoke": []}

        class R:
            text = "答え"
            session_id = None
            events = []
            meta = {}

        def fake_invoke(prompt, **kwargs):
            calls["invoke"].append((prompt, kwargs))
            return R()

        def fake_extract(q, **k):
            calls["extract"] += 1
            return ["kw"]

        def fake_search(db_path, keywords, **k):
            calls["search"].append(k)
            return [{"id": 1, "channel_id": 5, "channel": "c", "author": "a",
                     "content": "x", "created_at": "2026", "imgs": 0,
                     "vids": 0, "atts": 0}]

        with patch.object(invoke_claude, "invoke", fake_invoke), \
                patch.object(search, "extract_keywords", fake_extract), \
                patch.object(search, "search_messages", fake_search):
            runner_answer.answer_question(
                self.db_path, "1", "質問", agent=search.DEFAULT_AGENT, **kw)
        return calls

    def test_default_keeps_injection_and_v3(self):
        c = self._run(mcp_config="{}")
        self.assertEqual(c["extract"], 1)
        self.assertEqual(c["search"][0]["limit"], 24)
        prompt, kwargs = c["invoke"][-1]
        self.assertIn("【関連メッセージ】は社内ログから自動検索", kwargs["system"])
        # ツール注記は【質問】の直後（生成に最も近い位置）
        self.assertIn("【ツールの使いどころ】", prompt)
        self.assertLess(prompt.index("【質問】"),
                        prompt.index("【ツールの使いどころ】"))

    def test_no_injection_skips_keywords_and_uses_v4(self):
        c = self._run(mcp_config="{}", inject_search_hits=0,
                      inject_facts=False, prompt_style="v4")
        self.assertEqual(c["extract"], 0)
        self.assertEqual(c["search"], [])
        prompt, kwargs = c["invoke"][-1]
        self.assertIn("目的: チームの仕事を前に進める", kwargs["system"])
        self.assertNotIn("【関連メッセージ】は質問語", kwargs["system"])
        self.assertNotIn("【関連メッセージ】", prompt)
        self.assertIn("【ツールの使いどころ】", prompt)

    def test_reduced_injection_v4_notes_it(self):
        c = self._run(mcp_config="{}", inject_search_hits=6, prompt_style="v4")
        self.assertEqual(c["search"][0]["limit"], 6)
        self.assertIn("粗い1回検索の結果", c["invoke"][-1][1]["system"])

    def test_without_mcp_always_extracts(self):
        # ツールが無い経路では注入が唯一の根拠なので、設定に関わらず抽出する
        c = self._run(inject_search_hits=0, inject_facts=False)
        self.assertEqual(c["extract"], 1)
        self.assertNotIn("【ツールの使いどころ】", c["invoke"][-1][0])


class SelfReviewEvidenceTest(unittest.TestCase):
    def test_summary_and_prompt(self):
        events = [
            _tool_use("a", "mcp__archive__search_messages", {"keywords": ["組織図"]}),
            _tool_result("a", {"ok": True, "hits": 7, "message": "[1] 組織図 …"}),
            _tool_use("b", "mcp__archive__update_task", {"id": 6, "action": "done"}),
            _tool_result("b", {"ok": False, "error": "担当外"}),
        ]
        summary = evidence.summarize_for_review(events)
        self.assertIn("search_messages", summary)
        self.assertIn("hits=7", summary)
        self.assertIn("update_task", summary)
        self.assertIn("失敗: 担当外", summary)
        p = self_review.build_prompt("メンバー何人？", "9人です", evidence=summary)
        self.assertIn("【根拠", p)
        self.assertIn("根拠のない断定」とみなさない", p)
        self.assertNotIn("【根拠", self_review.build_prompt("q", "a"))

    def test_review_passes_evidence(self):
        seen = {}

        def fn(prompt):
            seen["p"] = prompt
            return '{"score": 5, "issue": ""}'
        r = self_review.review("q", "a" * 120, model="m", invoke_fn=fn,
                               evidence="- search_messages → ok hits=3")
        self.assertEqual(r["score"], 5)
        self.assertIn("hits=3", seen["p"])


class ObserveToolsTest(_TmpDb):
    def test_decide_reply_appends_tool_note_when_mcp(self):
        seen = {}
        cand = {"kind": "recall", "message_id": 10, "search_terms": ["納期"],
                "reason": "疑問"}
        trigger = {"id": 10, "channel_id": 1, "channel": "general",
                   "author_id": 1, "author": "人A", "content": "納期いつだっけ",
                   "created_at": "2026-07-31T11:00:00"}
        hit = {"id": 5, "channel_id": 2, "channel": "定例", "author": "人A",
               "content": "納期は8/8で確定", "created_at": "2026-07-20T10:00:00",
               "imgs": 0, "vids": 0, "atts": 0}

        def fn(p):
            seen["p"] = p
            return proactive.SILENT_TOKEN
        proactive.decide_reply(self.db_path, "9", "agent1", cand, trigger,
                               persona="", agent_name="エージェント",
                               invoke_fn=fn, search_fn=lambda k: [hit],
                               mcp_config='{"x":1}')
        self.assertIn("【ツール】", seen["p"])
        proactive.decide_reply(self.db_path, "9", "agent1", cand, trigger,
                               persona="", agent_name="エージェント",
                               invoke_fn=fn, search_fn=lambda k: [hit])
        self.assertNotIn("【ツール】", seen["p"])


class GoldenEvalTest(unittest.TestCase):
    def test_history_restored_from_archive(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db.init_db(path)
            with db.connect(path) as conn:
                conn.execute("INSERT INTO users(id, display_name, is_bot) VALUES(1,'人',0)")
                conn.execute("INSERT INTO users(id, display_name, is_bot) VALUES(2,'AI',1)")
                for mid, aid, c in [(10, 1, "前の話"), (11, 2, "了解"),
                                    (12, 1, "質問"), (13, 2, "答え")]:
                    conn.execute(
                        "INSERT INTO messages(id, channel_id, author_id, content, "
                        "created_at, deleted) VALUES(?,5,?,?, 'x', 0)",
                        (mid, aid, c))
                hist = golden_eval.history_for(conn, 5, 13)
        self.assertEqual([h["content"] for h in hist], ["前の話", "了解"])
        self.assertTrue(hist[1]["is_bot"])

    def test_classify_reference(self):
        self.assertEqual(golden_eval.classify_reference("了解です、転送しておきますね"),
                         "action")
        self.assertEqual(golden_eval.classify_reference("了解です\n-# 登録: id=42 …"),
                         "action")
        self.assertEqual(golden_eval.classify_reference(
            "9/12(土) 10:00〜 会場Aです。参照: https://discord.com/…"), "info")

    def test_select_and_grade_parse(self):
        rows = [{"id": i} for i in range(10)]
        picked = golden_eval.select_rows(rows, n=3, seed=1)
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked, golden_eval.select_rows(rows, n=3, seed=1))
        self.assertEqual(golden_eval.select_rows(rows)[0]["id"], 0)
        self.assertEqual(golden_eval.parse_grade('{"score": 4, "note": "ok"}'),
                         {"score": 4.0, "note": "ok"})
        self.assertIsNone(golden_eval.parse_grade("no json"))
        self.assertIsNone(golden_eval.parse_grade('{"score": 9}'))

    def test_evaluate_with_fakes_and_report(self):
        rows = [{"id": 1, "question": "q1", "answer": "会場はAです。詳細はリンク参照",
                 "channel_id": 5},
                {"id": 2, "question": "q2", "answer": "9/12 10時にAで開催です",
                 "channel_id": 5}]
        logs = []
        rep = golden_eval.evaluate(
            ":memory:", {"id": "agent1", "name": "エージェント", "persona_files": []},
            rows, tools=True,
            answer_fn=lambda r: (f"ans-{r['id']}", {"tool_calls": 1, "cost_usd": 0.1}),
            grade_fn=lambda r, a: '{"score": 4, "note": "良い"}' if r["id"] == 1
            else "壊れた出力", log=logs.append)
        self.assertEqual(rep["n"], 2)
        self.assertEqual(rep["scored"], 1)
        self.assertEqual(rep["mean"], 4.0)
        self.assertEqual(rep["items"][1]["note"], "採点不能")
        with tempfile.TemporaryDirectory() as d:
            path = golden_eval.save_report(rep, out_dir=d)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            # 週次レポートが読む形（mean / scored）
            self.assertIsNotNone(proactive.golden_eval_latest(d))
        self.assertEqual(saved["mean"], 4.0)
        self.assertEqual(saved["scored"], 1)

    def test_load_rows_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db.init_db(path)
            with db.connect(path) as conn:
                conn.execute(
                    "INSERT INTO golden_set(agent_id, question, answer, status) "
                    "VALUES('agent1','q','a','active')")
                rows = golden_eval.load_rows(conn, "agent1", "curated")
        self.assertEqual([r["question"] for r in rows], ["q"])


if __name__ == "__main__":
    unittest.main()
