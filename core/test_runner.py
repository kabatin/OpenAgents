#!/usr/bin/env python3
"""runner/invoke_claude・runner_answer のユニットテスト。"""

from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import tempfile
import unittest

from core import invoke_claude
from core import runner_answer
from core import search

#: 検索を差し替えるので中身は使われない。それでも sqlite が触るので、
#: リポジトリを汚さないよう一時ファイルにする
_TMP_DB = os.path.join(tempfile.gettempdir(), "openagents-test.db")


def _ev(obj):
    return json.dumps(obj, ensure_ascii=False)


class ParseStreamEventsTest(unittest.TestCase):
    def test_text_only(self):
        lines = [
            _ev({"type": "system", "subtype": "init"}),
            _ev({"type": "assistant",
                 "message": {"content": [{"type": "text", "text": "こんにちは"}]}}),
            _ev({"type": "result", "subtype": "success", "result": "こんにちは"}),
        ]
        events, final, texts, err = invoke_claude.parse_stream_events(lines)
        self.assertEqual(len(events), 3)
        self.assertEqual(final, "こんにちは")
        self.assertEqual(texts, ["こんにちは"])
        self.assertIsNone(err)

    def test_body_in_intermediate_message_survives(self):
        # 既知の落とし穴: ツール実行を挟むと本文が中間メッセージに来る。
        # stream-json解釈で失われないこと（lessons.md の再発防止）
        lines = [
            _ev({"type": "assistant",
                 "message": {"content": [
                     {"type": "text", "text": "本文はこちら"},
                     {"type": "tool_use", "id": "t1", "name": "Read",
                      "input": {"file_path": "a.txt"}}]}}),
            _ev({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1"}]}}),
            _ev({"type": "result", "subtype": "success", "result": ""}),
        ]
        _, final, texts, err = invoke_claude.parse_stream_events(lines)
        self.assertEqual(final, "")
        self.assertEqual(texts, ["本文はこちら"])
        self.assertIsNone(err)
        r = invoke_claude.InvokeResult([], final, texts)
        self.assertEqual(r.text, "本文はこちら")  # finalが空ならassistant本文

    def test_final_text_preferred(self):
        r = invoke_claude.InvokeResult([], "最終", ["途中", "最終"])
        self.assertEqual(r.text, "最終")

    def test_error_result_detected(self):
        lines = [_ev({"type": "result", "subtype": "error_max_turns",
                      "is_error": True, "result": "turn limit"})]
        _, _, _, err = invoke_claude.parse_stream_events(lines)
        self.assertEqual(err, "turn limit")

    def test_garbage_lines_ignored(self):
        lines = ["", "not json", "[1,2,3]",
                 _ev({"type": "result", "subtype": "success", "result": "ok"})]
        events, final, _, err = invoke_claude.parse_stream_events(lines)
        self.assertEqual(len(events), 1)
        self.assertEqual(final, "ok")
        self.assertIsNone(err)


class RunnerAnswerBranchTest(unittest.TestCase):
    """search.answer_question とのパリティ（分岐単位・claude不要）。"""

    def setUp(self):
        self.calls = []
        test = self

        class FakeResult:
            def __init__(self, text):
                self.text = text

        def fake_invoke(prompt, **kwargs):
            test.calls.append({"prompt": prompt, **kwargs})
            return FakeResult("回答です")

        p = patch.object(runner_answer.invoke_claude, "invoke", fake_invoke)
        p.start()
        self.addCleanup(p.stop)

    def test_no_hits_general_route(self):
        with patch.object(search, "extract_keywords", return_value=["kw"]), \
                patch.object(search, "search_messages", return_value=[]):
            out = runner_answer.answer_question(_TMP_DB, "1", "こんにちは")
        self.assertEqual(out, {"answer": "回答です", "keywords": ["kw"],
                               "hits": 0, "session_id": None})
        call = self.calls[-1]
        self.assertIn("アシスタント", call["system"])   # ペルソナ/方針はsystem側
        self.assertNotIn("【関連メッセージ】", call["prompt"])
        # 添付なしでも Web検索/取得は常時利用可（Readは付かない）
        self.assertEqual(call["allowed_tools"], runner_answer.WEB_TOOLS)
        self.assertNotIn("cwd", call)

    def test_hits_answer_route(self):
        rows = [{"id": 1, "channel_id": 2, "channel": "general",
                 "author": "人A", "content": "こんにちは",
                 "created_at": "2026-07-16T00:00:00",
                 "imgs": 0, "vids": 0, "atts": 0}]
        with patch.object(search, "extract_keywords", return_value=["kw"]), \
                patch.object(search, "search_messages", return_value=rows):
            out = runner_answer.answer_question(_TMP_DB, "1", "質問")
        self.assertEqual(out["hits"], 1)
        call = self.calls[-1]
        self.assertIn("【関連メッセージ】", call["prompt"])
        self.assertIn("参照", call["system"])            # ANSWER側テンプレ

    def test_silent_attachment_skips_search(self):
        att = SimpleNamespace(block="【添付ファイル】note.txt",
                              has_supported=True, dir="/tmp/att")
        boom = AssertionError("検索は呼ばれてはいけない")
        with patch.object(search, "extract_keywords", side_effect=boom), \
                patch.object(search, "search_messages", side_effect=boom):
            out = runner_answer.answer_question(_TMP_DB, "1", "",
                                                attachments=att)
        self.assertEqual(out["hits"], 0)
        call = self.calls[-1]
        # 添付ありでは Read + WebSearch のみ（WebFetchは流出防止で外す）
        self.assertEqual(call["allowed_tools"],
                         ("Read",) + runner_answer.WEB_TOOLS_NO_FETCH)
        self.assertNotIn("WebFetch", call["allowed_tools"])
        self.assertEqual(call["cwd"], "/tmp/att")
        self.assertEqual(call["timeout"], runner_answer.ATTACH_TIMEOUT_SEC)
        self.assertIn(runner_answer.ATTACH_DEFAULT_QUESTION, call["prompt"])
        self.assertIn("【添付ファイル】note.txt", call["prompt"])

    def test_web_tools_always_available(self):
        # 添付なしの通常回答でも Web検索/取得は常時利用可（事前承認つき）
        with patch.object(search, "extract_keywords", return_value=["kw"]), \
                patch.object(search, "search_messages", return_value=[]):
            runner_answer.answer_question(_TMP_DB, "1", "最新ニュース教えて")
        call = self.calls[-1]
        self.assertEqual(call["allowed_tools"], runner_answer.WEB_TOOLS)
        self.assertEqual(call["allow"], runner_answer.WEB_TOOLS)
        self.assertNotIn("cwd", call)  # 添付なしなのでcwd閉じ込めなし
        self.assertIn("Web検索", call["system"])  # スキル告知がsystemに載る


class RunnerAnswerBuildPromptTest(unittest.TestCase):
    def test_all_blocks(self):
        p = runner_answer.build_prompt(
            "質問です", "人A: こんにちは", "・要約1本",
            "[1] (#general, 人A, 2026-07-16)", "【添付】note.txt")
        self.assertIn("【このチャンネルの文脈要約】\n・要約1本", p)
        self.assertIn("【直近の会話】\n人A: こんにちは", p)
        self.assertIn("【質問】\n質問です", p)
        self.assertIn("【関連メッセージ】", p)
        self.assertTrue(p.endswith("【添付】note.txt"))
        # 要約→会話→質問→関連 の順序
        self.assertLess(p.index("文脈要約"), p.index("直近の会話"))
        self.assertLess(p.index("直近の会話"), p.index("【質問】"))

    def test_minimal(self):
        p = runner_answer.build_prompt("質問だけ", "", "", None, "")
        self.assertEqual(p, "【質問】\n質問だけ")

    def test_agent_context_comes_first(self):
        p = runner_answer.build_prompt("質問", "", "", None, "",
                                       facts_block="【事実台帳】x",
                                       agent_context="【発言者】A: 代表")
        self.assertTrue(p.startswith("【この会話の前提】\n【発言者】A: 代表"))
        self.assertLess(p.index("この会話の前提"), p.index("事実台帳"))


class CacheStabilityTest(unittest.TestCase):
    """固定費対策: 設定ファイルを読まず、会話ごとの前提を system に入れない。"""

    def test_argv_has_setting_sources_and_dynamic_exclusion(self):
        argv = invoke_claude.build_argv("claude", model="m")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv)
        # 従来の契約は据え置き（ツール無し・stream-json）
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")

    def test_context_goes_to_user_prompt_not_system(self):
        captured = {}

        class R:
            text = "a"
            session_id = None
            events = []
            meta = {}

        def fake_invoke(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["system"] = kwargs.get("system")
            return R()

        agent = dict(search.DEFAULT_AGENT, role="役割の文",
                     context="【発言者】A: 代表")
        with patch.object(runner_answer.invoke_claude, "invoke", fake_invoke), \
                patch.object(search, "extract_keywords", return_value=["kw"]), \
                patch.object(search, "search_messages", return_value=[]):
            runner_answer.answer_question(_TMP_DB, "1", "質問", agent=agent)
        self.assertIn("役割の文", captured["system"])
        self.assertNotIn("A: 代表", captured["system"])
        self.assertIn("【この会話の前提】", captured["prompt"])
        self.assertIn("A: 代表", captured["prompt"])

    def test_legacy_route_keeps_context_in_role(self):
        # 旧経路（search.answer_question）は context を role に合流させる
        agent = dict(search.DEFAULT_AGENT, role="役割の文",
                     context="【発言者】A: 代表")
        system = search._build_system(search.GENERAL_SYSTEM_TMPL, agent)
        self.assertIn("役割の文", system)
        self.assertIn("A: 代表", system)


class ToolLoopArgvTest(unittest.TestCase):
    """v4 ツールループ: mcp_config / strict / budget / on_event の配線。"""

    def test_mcp_config_adds_settings_strict_and_budget(self):
        argv = invoke_claude.build_argv(
            "claude", model="m", mcp_config='{"mcpServers":{}}',
            allow=("mcp__archive__search_messages",), max_budget_usd=0.5)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--permission-mode", argv)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings["permissions"]["allow"],
                         ["mcp__archive__search_messages"])
        i = argv.index("--mcp-config")
        self.assertEqual(argv[i + 2], "--strict-mcp-config")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.5")

    def test_no_mcp_no_tools_means_no_settings(self):
        argv = invoke_claude.build_argv("claude", model="m")
        self.assertNotIn("--settings", argv)
        self.assertNotIn("--strict-mcp-config", argv)
        self.assertNotIn("--max-budget-usd", argv)

    def test_streaming_route_calls_on_event(self):
        import os
        import sys
        import tempfile
        script = (
            "import sys\n"
            "sys.stdin.read()\n"
            "print('{\"type\":\"assistant\",\"message\":{\"content\":"
            "[{\"type\":\"tool_use\",\"id\":\"t\",\"name\":\"mcp__archive__x\","
            "\"input\":{}}]}}')\n"
            "print('garbage')\n"
            "print('{\"type\":\"result\",\"subtype\":\"success\","
            "\"result\":\"done\",\"num_turns\":2}')\n")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fake.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            seen = []
            proc = invoke_claude._run_streaming(
                [sys.executable, path], "prompt", timeout=30, cwd=None,
                on_event=seen.append)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual([e["type"] for e in seen], ["assistant", "result"])
        events, final, _texts, err = invoke_claude.parse_stream_events(
            proc.stdout.splitlines())
        self.assertEqual(final, "done")
        self.assertIsNone(err)
        self.assertEqual(invoke_claude.extract_meta(events)["tool_calls"], 1)

    def test_streaming_route_times_out(self):
        import subprocess
        import sys
        with self.assertRaises(subprocess.TimeoutExpired):
            invoke_claude._run_streaming(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                "", timeout=0.3, cwd=None, on_event=lambda ev: None)
