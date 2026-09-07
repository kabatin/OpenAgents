#!/usr/bin/env python3
"""v4 Phase 0 計測: invoke_claude の meta 抽出／RECORDER、db.llm_calls、logstamp。"""

from types import SimpleNamespace
import json
import os
import subprocess
import tempfile
import unittest

from core import db
from core import invoke_claude
from core import logstamp


def _ev(obj):
    return json.dumps(obj, ensure_ascii=False)


RESULT_EV = {
    "type": "result", "subtype": "success", "result": "4242",
    "duration_ms": 3742, "duration_api_ms": 3582, "num_turns": 2,
    "total_cost_usd": 0.0256, "stop_reason": "end_turn",
    "permission_denials": [],
    "usage": {"input_tokens": 9, "output_tokens": 198,
              "cache_read_input_tokens": 24000,
              "cache_creation_input_tokens": 300,
              "output_tokens_details": {"thinking_tokens": 192}},
    "modelUsage": {"claude-haiku-4-5-20251001": {"costUSD": 0.0256}},
}


class ExtractMetaTest(unittest.TestCase):
    def test_result_fields_and_tool_calls(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__archive__search", "input": {}},
                {"type": "text", "text": "本文"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__archive__search", "input": {}}]}},
            RESULT_EV,
        ]
        m = invoke_claude.extract_meta(events)
        self.assertEqual(m["cost_usd"], 0.0256)
        self.assertEqual(m["duration_ms"], 3742)
        self.assertEqual(m["num_turns"], 2)
        self.assertEqual(m["input_tokens"], 9)
        self.assertEqual(m["cache_read_tokens"], 24000)
        self.assertEqual(m["thinking_tokens"], 192)
        self.assertEqual(m["stop_reason"], "end_turn")
        self.assertEqual(m["denials"], 0)
        self.assertEqual(m["tool_calls"], 2)
        self.assertEqual(m["tools_used"], ["mcp__archive__search"])
        self.assertEqual(m["models"], ["claude-haiku-4-5-20251001"])

    def test_no_result_event_leaves_none(self):
        # 失敗起動（result が来ない）では 0 ではなく None（不明）を残す
        m = invoke_claude.extract_meta([{"type": "system"}])
        self.assertIsNone(m["cost_usd"])
        self.assertIsNone(m["duration_ms"])
        self.assertEqual(m["tool_calls"], 0)

    def test_denials_counted(self):
        ev = dict(RESULT_EV, permission_denials=[{"tool_name": "x"}])
        self.assertEqual(invoke_claude.extract_meta([ev])["denials"], 1)


class RecorderTest(unittest.TestCase):
    """RECORDER は成功・失敗を問わず1起動1回呼ばれ、例外を投げても本流を止めない。"""

    def setUp(self):
        self.records = []
        self._orig_run = invoke_claude.subprocess.run
        self._orig_rec = invoke_claude.RECORDER
        self._orig_bin = invoke_claude._claude_bin
        self._orig_avail = invoke_claude.check_available
        invoke_claude._claude_bin = lambda: "/bin/sh"  # 存在するパス
        invoke_claude.check_available = lambda cfg=None: None
        invoke_claude.RECORDER = self.records.append

    def tearDown(self):
        invoke_claude.subprocess.run = self._orig_run
        invoke_claude.RECORDER = self._orig_rec
        invoke_claude._claude_bin = self._orig_bin
        invoke_claude.check_available = self._orig_avail

    def _fake(self, stdout="", returncode=0, stderr=""):
        def run(cmd, **kw):
            return SimpleNamespace(returncode=returncode, stdout=stdout,
                                   stderr=stderr)
        invoke_claude.subprocess.run = run

    def test_success_recorded_with_purpose_and_agent(self):
        self._fake(stdout=_ev(RESULT_EV))
        token = invoke_claude.CURRENT_AGENT.set("agent1")
        try:
            r = invoke_claude.invoke("q", model="m", purpose="answer")
        finally:
            invoke_claude.CURRENT_AGENT.reset(token)
        self.assertEqual(r.text, "4242")
        self.assertEqual(r.meta["cost_usd"], 0.0256)
        self.assertEqual(len(self.records), 1)
        rec = self.records[0]
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["purpose"], "answer")
        self.assertEqual(rec["model"], "m")
        self.assertEqual(rec["agent_id"], "agent1")
        self.assertEqual(rec["cost_usd"], 0.0256)
        self.assertIsInstance(rec["wall_ms"], int)

    def test_exit_failure_recorded(self):
        self._fake(returncode=1, stderr="boom")
        with self.assertRaises(RuntimeError):
            invoke_claude.invoke("q", purpose="screen")
        self.assertEqual(len(self.records), 1)
        self.assertFalse(self.records[0]["ok"])
        self.assertIn("boom", self.records[0]["error"])
        self.assertEqual(self.records[0]["purpose"], "screen")

    def test_timeout_recorded_and_reraised(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 5)
        invoke_claude.subprocess.run = run
        with self.assertRaises(subprocess.TimeoutExpired):
            invoke_claude.invoke("q", timeout=5)
        self.assertFalse(self.records[0]["ok"])
        self.assertIn("timeout", self.records[0]["error"])

    def test_recorder_exception_does_not_break_answer(self):
        def bad(rec):
            raise RuntimeError("db down")
        invoke_claude.RECORDER = bad
        self._fake(stdout=_ev(RESULT_EV))
        self.assertEqual(invoke_claude.invoke("q").text, "4242")

    def test_no_recorder_is_fine(self):
        invoke_claude.RECORDER = None
        self._fake(stdout=_ev(RESULT_EV))
        self.assertEqual(invoke_claude.invoke("q").text, "4242")


class LlmCallsDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_ignores_unknown_keys_and_summarizes(self):
        with db.connect(self.path) as conn:
            rid = db.add_llm_call(
                conn, agent_id="agent1", purpose="answer", model="m", ok=True,
                cost_usd=0.02, duration_ms=1200, tools_used=["x"],  # 未知キー
                created_at="2026-09-05T10:00")
            db.add_llm_call(
                conn, agent_id="agent1", purpose="screen", model="h", ok=False,
                error="exit=1", created_at="2026-09-05T11:00")
            db.add_llm_call(
                conn, agent_id="agent1", purpose="answer", model="m", ok=True,
                cost_usd=0.03, duration_ms=800, created_at="2026-09-04T09:00")
            self.assertEqual(rid, 1)
            rows = db.llm_daily(conn, "2026-09-01T00:00")
        self.assertEqual([r["day"] for r in rows], ["2026-09-05", "2026-09-04"])
        today = rows[0]
        self.assertEqual(today["calls"], 2)
        self.assertEqual(today["failed"], 1)
        self.assertAlmostEqual(today["cost_usd"], 0.02)
        self.assertEqual(today["max_ms"], 1200)


class LogstampTest(unittest.TestCase):
    def test_stamps_only_line_heads(self):
        out, at_start = logstamp.stamp_lines("a\nb", True, "T")
        self.assertEqual(out, "T a\nT b")
        self.assertFalse(at_start)  # "b" は改行で終わっていない
        out2, at_start2 = logstamp.stamp_lines("c\n", at_start, "T")
        self.assertEqual(out2, "c\n")  # 途中行の続きには印を挟まない
        self.assertTrue(at_start2)

    def test_blank_lines_untouched(self):
        out, _ = logstamp.stamp_lines("\n\nx\n", True, "T")
        self.assertEqual(out, "\n\nT x\n")

    def test_empty(self):
        self.assertEqual(logstamp.stamp_lines("", True, "T"), ("", True))

    def test_install_is_idempotent(self):
        import io
        import sys
        orig_out, orig_err = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            logstamp.install()
            first = sys.stdout
            logstamp.install()
            self.assertIs(sys.stdout, first)  # 二重に包まない
            print("hello")
            self.assertRegex(first._stream.getvalue(),
                             r"^\d\d-\d\d \d\d:\d\d:\d\d hello\n$")
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err


if __name__ == "__main__":
    unittest.main()
