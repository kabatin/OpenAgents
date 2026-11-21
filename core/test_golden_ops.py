#!/usr/bin/env python3
"""操作の回帰チェック（golden_ops）と、書き込みツールの予行のテスト。

実行: ./venv/bin/python -m unittest core.test_golden_ops
"""

import json
import unittest
from datetime import datetime

from core import golden_ops
from core.archive_tools import registry
from core.archive_tools.context import ToolContext

NOW = datetime(2026, 11, 10, 19, 50)


def _call(name, **args):
    return {"name": name, "input": args}


def _case(case_id):
    return next(c for c in golden_ops.CASES if c["id"] == case_id)


class CheckTest(unittest.TestCase):

    def test_tomorrow_reminder_passes(self):
        res = golden_ops.check(_case("remind-tomorrow"), [_call(
            "add_reminder", due="2026-11-11 15:00", content="請求書の確認")], NOW)
        self.assertTrue(res["pass"], res)

    def test_wrong_day_fails(self):
        res = golden_ops.check(_case("remind-tomorrow"), [_call(
            "add_reminder", due="2026-11-12 15:00", content="請求書の確認")], NOW)
        self.assertFalse(res["pass"])
        self.assertIn("引数が違う", res["why"])

    def test_not_calling_the_tool_fails(self):
        res = golden_ops.check(_case("remind-tomorrow"), [_call("list_reminders")], NOW)
        self.assertEqual(res, {"pass": False, "why": "add_reminder を呼ばなかった"})

    def test_relative_time_needs_the_clock(self):
        case = _case("remind-30min")
        ok = golden_ops.check(case, [_call(
            "add_reminder", due="2026-11-10 20:20", content="会議室の予約")], NOW)
        self.assertTrue(ok["pass"], ok)
        # 時刻を知らずに当て推量で入れた日時は通さない
        guess = golden_ops.check(case, [_call(
            "add_reminder", due="2026-11-10 12:30", content="会議室の予約")], NOW)
        self.assertFalse(guess["pass"])

    def test_weekly_reminder_needs_the_repeat(self):
        case = _case("remind-weekly")
        full = dict(due="2026-11-11 10:00", repeat="weekly",
                    content="備品の在庫チェック")
        self.assertTrue(golden_ops.check(case, [_call("add_reminder", **full)], NOW)["pass"])
        once = {k: v for k, v in full.items() if k != "repeat"}
        self.assertFalse(golden_ops.check(case, [_call("add_reminder", **once)], NOW)["pass"])

    def test_every_expected_call_is_required(self):
        case = _case("glossary-two")
        one = [_call("save_glossary", wrong="アーカイプ", correct="アーカイブ")]
        self.assertFalse(golden_ops.check(case, one, NOW)["pass"])
        both = one + [_call("save_glossary", wrong="ダッシュボート",
                                  correct="ダッシュボード")]
        self.assertTrue(golden_ops.check(case, both, NOW)["pass"])

    def test_look_only_request_must_not_write(self):
        case = _case("remind-list")
        self.assertTrue(golden_ops.check(case, [_call("list_reminders")], NOW)["pass"])
        res = golden_ops.check(case, [_call("list_reminders"),
                                      _call("cancel_reminder", id="50")], NOW)
        self.assertFalse(res["pass"])
        self.assertIn("cancel_reminder", res["why"])

    def test_every_case_names_real_tools(self):
        """ツール名や引数名を変えたのに問題が古いまま、を落とす。"""
        registry.ensure_loaded()
        tools = {t.name: t for t in registry.all_tools()}
        for case in golden_ops.CASES:
            for name, conds in case["expect"]:
                self.assertIn(name, tools, case["id"])
                props = tools[name].input_schema.get("properties", {})
                for key in conds:
                    self.assertIn(key, props, f"{case['id']}: {name}.{key}")


class ApplicableTest(unittest.TestCase):

    def test_cases_needing_invisible_tools_are_left_out(self):
        """リマインダーのスキルが無いエージェントに、登録の問題は出さない。"""
        visible = {"save_glossary", "save_fact", "save_rule", "search_messages"}
        ids = [c["id"] for c in golden_ops.applicable(golden_ops.CASES, visible)]
        self.assertEqual(ids, ["glossary-two", "fact-correction", "rule-from-now-on"])

    def test_all_cases_stay_when_everything_is_visible(self):
        registry.ensure_loaded()
        visible = {t.name for t in registry.all_tools()}
        self.assertEqual(golden_ops.applicable(golden_ops.CASES, visible),
                         golden_ops.CASES)


class EvaluateTest(unittest.TestCase):

    @staticmethod
    def _events(name, args):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1",
                 "name": f"mcp__archive__{name}", "input": args}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": json.dumps({"ok": True})}]}]}}]

    def test_counts_passes_and_keeps_going_after_an_error(self):
        cases = [_case("remind-tomorrow"), _case("remind-list"), _case("rule-from-now-on")]

        def answer(case):
            if case["id"] == "remind-list":
                raise RuntimeError("timeout")
            if case["id"] == "remind-tomorrow":
                return self._events("add_reminder", {
                    "due": "2026-11-11 15:00", "content": "請求書の確認"})
            return self._events("search_messages", {"query": "絵文字"})

        rep = golden_ops.evaluate("unused.db", {}, {}, cases=cases, answer_fn=answer,
                                  now_fn=lambda: NOW, log=lambda *_: None)
        self.assertEqual((rep["passed"], rep["n"]), (1, 3))
        self.assertEqual([i["pass"] for i in rep["items"]], [True, False, False])
        self.assertIn("timeout", rep["items"][1]["why"])


class RedTest(unittest.TestCase):

    def test_one_failure_is_within_noise(self):
        items = [{"id": "a", "pass": True}, {"id": "b", "pass": False, "why": "x"}]
        self.assertIsNone(golden_ops.red(items))

    def test_two_failures_are_red(self):
        items = [{"id": "a", "pass": False, "why": "add_reminder を呼ばなかった"},
                 {"id": "b", "pass": False, "why": "x"}, {"id": "c", "pass": True}]
        why = golden_ops.red(items)
        self.assertIn("1/3問", why)
        self.assertIn("a: add_reminder を呼ばなかった", why)

    def test_red_is_filed_to_the_ledger(self):
        import os
        import tempfile
        from core import db
        from core import golden_eval
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        db.init_db(path)
        items = [{"id": "a", "pass": False, "why": "x"}, {"id": "b", "pass": False, "why": "y"}]
        report = {"config": {"agent": "agent1"}, "ops": {"items": items}}
        self.assertIsNotNone(golden_eval.report_red_if_ops_failed(path, report, "r.json"))
        ok = {"config": {"agent": "agent1"}, "ops": {"items": [{"id": "a", "pass": True}]}}
        self.assertIsNone(golden_eval.report_red_if_ops_failed(path, ok, "r.json"))
        self.assertIsNone(golden_eval.report_red_if_ops_failed(path, {"config": {}}, "r.json"))
        with db.connect(path) as conn:
            rows = conn.execute("SELECT source, topic FROM misses").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("quality_drop", "quality:ops")])


class RehearseTest(unittest.TestCase):
    """予行: 書き込みツールは見せるが、実行はしない。"""

    def _tools(self, calls):
        def handler(ctx, args):
            calls.append(args)
            return {"ok": True, "message": "done"}
        return [registry.Tool(name="w", description="", input_schema={},
                              kind="write", handler=handler),
                registry.Tool(name="r", description="", input_schema={},
                              kind="read", handler=handler)]

    def _ctx(self, **kw):
        return ToolContext(agent_id="agent1", actor_id="1", db_path=":memory:",
                           guild_id="1", **kw)

    def test_write_is_accepted_but_not_executed(self):
        calls = []
        res = registry.dispatch(self._ctx(rehearse=True), "w", {"a": 1}, self._tools(calls))
        self.assertTrue(res["ok"])
        self.assertTrue(res["rehearsed"])
        self.assertEqual(calls, [])

    def test_read_still_runs(self):
        calls = []
        registry.dispatch(self._ctx(rehearse=True), "r", {"q": 1}, self._tools(calls))
        self.assertEqual(calls, [{"q": 1}])

    def test_without_rehearse_the_write_runs(self):
        calls = []
        res = registry.dispatch(self._ctx(), "w", {"a": 1}, self._tools(calls))
        self.assertEqual(calls, [{"a": 1}])
        self.assertNotIn("rehearsed", res)

    def test_flag_survives_the_command_line(self):
        ctx = ToolContext.from_args(self._ctx(rehearse=True).to_args())
        self.assertTrue(ctx.rehearse)
        self.assertFalse(ToolContext.from_args(self._ctx().to_args()).rehearse)


if __name__ == "__main__":
    unittest.main()
