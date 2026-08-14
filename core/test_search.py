#!/usr/bin/env python3
"""search（回答生成・claude起動）のユニットテスト。"""

from types import SimpleNamespace
import os
import tempfile
import unittest

from core import attachments
from core import llm
from core import search

#: 検索を差し替えるので中身は使われない。それでも sqlite が触るので、
#: リポジトリを汚さないよう一時ファイルにする
_TMP_DB = os.path.join(tempfile.gettempdir(), "openagents-test.db")


class BuildSystemTest(unittest.TestCase):
    def test_role_inserted(self):
        s = search._build_system(
            search.GENERAL_SYSTEM_TMPL,
            {"name": "エージェント2", "role": "デザイン担当。"})
        self.assertIn("「エージェント2」です。\nデザイン担当。", s)

    def test_empty_role_matches_legacy(self):
        s = search._build_system(search.GENERAL_SYSTEM_TMPL,
                                 search.DEFAULT_AGENT)
        self.assertIn(f"「{search.DEFAULT_AGENT['name']}」です。\n"
                      "親しみやすく", s)


class _FakeBinary:
    """claude CLI の実在に依存しないためのパッチ。

    CI（クリーンな環境）には claude が入っていない。テストは
    「どんなコマンドを組み立てるか」を見るのであって、実物の存在を
    確かめるものではないので、探索も差し替える。
    """

    def __enter__(self):
        self._find = llm.find_binary
        llm.find_binary = lambda spec: "/fake/bin/claude"
        return self

    def __exit__(self, *exc):
        llm.find_binary = self._find


class RunClaudeTest(unittest.TestCase):
    def test_all_tools_disabled(self):
        # -p はツール実行を挟むと最終メッセージしか返さず本文が失われる。
        # --tools "" が常に付くことを保証する（YouTube要約欠落の再発防止）
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        orig = search.subprocess.run
        try:
            search.subprocess.run = fake_run
            with _FakeBinary():
                search.run_claude("テスト")
        finally:
            search.subprocess.run = orig
        cmd = captured["cmd"]
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")


class LoadPersonaTest(unittest.TestCase):
    def test_default_persona_template_is_readable(self):
        # 同梱テンプレートは設定ゼロでも読める（初回起動でここに落ちない）
        p = search.load_persona(search.DEFAULT_PERSONA_FILES)
        self.assertIn("## 話し方", p)
        self.assertIn("{{AGENT_NAME}}", p)

    def test_missing_files_skipped(self):
        self.assertEqual(search.load_persona(["/no/such/file.md"]), "")


class RunClaudeAttachmentTest(unittest.TestCase):
    def _capture(self, **kwargs):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        orig = search.subprocess.run
        try:
            search.subprocess.run = fake_run
            with _FakeBinary():
                search.run_claude("テスト", **kwargs)
        finally:
            search.subprocess.run = orig
        return captured

    def test_default_argv_unchanged(self):
        cap = self._capture()
        cmd = cap["cmd"]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--settings", cmd)
        self.assertIsNone(cap["kw"].get("cwd"))

    def test_allowed_tools_sets_read_guard_and_cwd(self):
        cap = self._capture(allowed_tools=("Read",), cwd="/tmp/att")
        cmd = cap["cmd"]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read")
        # cwd外Readの自動拒否（主防御。autoモードでは/etc等も読めてしまう）
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        # ホーム配下Read禁止のガード設定が必ず付くこと（二重防御）
        self.assertIn("--settings", cmd)
        self.assertIn("Read(~/**)", cmd[cmd.index("--settings") + 1])
        self.assertEqual(cap["kw"]["cwd"], "/tmp/att")


class AnswerQuestionAttachmentTest(unittest.TestCase):
    def _run(self, question, ctx):
        calls = {"extract": 0, "prompts": [], "kwargs": []}

        def fake_extract(q, **kw):
            calls["extract"] += 1
            return ["kw"]

        def fake_search(*a, **kw):
            return []

        def fake_run_claude(prompt, **kw):
            calls["prompts"].append(prompt)
            calls["kwargs"].append(kw)
            return "answer"

        orig = (search.extract_keywords, search.search_messages,
                search.run_claude)
        try:
            search.extract_keywords = fake_extract
            search.search_messages = fake_search
            search.run_claude = fake_run_claude
            result = search.answer_question(
                _TMP_DB, "1", question, attachments=ctx)
        finally:
            (search.extract_keywords, search.search_messages,
             search.run_claude) = orig
        return calls, result

    def test_empty_question_uses_default_and_skips_search(self):
        ctx = attachments.AttachmentContext(
            block="【添付ファイル】\n- /tmp/att/01-a.png",
            dir="/tmp/att", has_supported=True)
        calls, result = self._run("", ctx)
        self.assertEqual(calls["extract"], 0)  # キーワード抽出しない
        prompt = calls["prompts"][0]
        self.assertIn(attachments.DEFAULT_QUESTION, prompt)
        self.assertIn("【添付ファイル】", prompt)
        kw = calls["kwargs"][0]
        self.assertEqual(kw.get("allowed_tools"), ("Read",))
        self.assertEqual(kw.get("cwd"), "/tmp/att")
        self.assertEqual(kw.get("timeout"), attachments.TIMEOUT_SEC)
        self.assertEqual(result["hits"], 0)

    def test_unsupported_only_gets_no_tools(self):
        ctx = attachments.AttachmentContext(
            block="【添付ファイル】\n- x.docx → 読めない",
            dir="/tmp/att", has_supported=False)
        calls, _ = self._run("これ読める？", ctx)
        kw = calls["kwargs"][0]
        self.assertNotIn("allowed_tools", kw)
        self.assertNotIn("cwd", kw)
        self.assertIn("【添付ファイル】", calls["prompts"][0])

    def test_no_attachments_prompt_clean(self):
        calls, _ = self._run("こんにちは", None)
        self.assertNotIn("【添付ファイル】", calls["prompts"][0])
        self.assertNotIn("allowed_tools", calls["kwargs"][0])


class ConfidenceNoteTest(unittest.TestCase):
    """自信度の明示（RM#83）: 回答テンプレが低確信の自己申告を教えている。"""

    def test_templates_teach_confidence_note(self):
        for tmpl in (search.ANSWER_SYSTEM_TMPL, search.GENERAL_SYSTEM_TMPL):
            self.assertIn("自信度低め", tmpl)
            self.assertIn("断定", tmpl)
