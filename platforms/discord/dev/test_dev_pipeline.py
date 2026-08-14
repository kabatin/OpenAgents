#!/usr/bin/env python3
"""dev_pipeline の純粋関数テスト（claude/gitは起動しない）。

実行: ../chatbot/venv/bin/python -m unittest test_dev_pipeline -v
"""

import unittest

from platforms.discord.dev import dev_pipeline


class ParseCommandTest(unittest.TestCase):
    def test_dev_bracket(self):
        self.assertEqual(dev_pipeline.parse_dev_command("[DEV: 4]"), 4)

    def test_kihyo_hash(self):
        self.assertEqual(dev_pipeline.parse_dev_command("起票 #12 やって"), 12)

    def test_kihyo_no_hash(self):
        self.assertEqual(dev_pipeline.parse_dev_command("起票7 実装して"), 7)

    def test_ignores_plain_text(self):
        self.assertIsNone(dev_pipeline.parse_dev_command("こんにちは"))
        self.assertIsNone(dev_pipeline.parse_dev_command("!status"))
        self.assertIsNone(dev_pipeline.parse_dev_command("起票して"))  # 数字なし


class WantsFreshStartTest(unittest.TestCase):
    def test_detects_fresh_keywords(self):
        for text in ("起票 #7 作り直し", "起票7 作りなおして", "ゼロから作って"):
            self.assertTrue(dev_pipeline.wants_fresh_start(text), text)

    def test_default_is_resume(self):
        self.assertFalse(dev_pipeline.wants_fresh_start("起票 #7 やって"))
        self.assertFalse(dev_pipeline.wants_fresh_start(None))

    def test_natural_phrases_do_not_destroy_worktree(self):
        # worktree破棄は不可逆なので、自然文の一部で誤爆しない
        for text in ("最初からテストを書いてほしい、起票 #7 やって",
                     "設計をやり直した方がいいかも。起票 #7 やって"):
            self.assertFalse(dev_pipeline.wants_fresh_start(text), text)


class ClassifyEventTest(unittest.TestCase):
    @staticmethod
    def _asst(blocks):
        return {"type": "assistant", "message": {"content": blocks}}

    def test_edit_shows_basename(self):
        ev = self._asst([{"type": "tool_use", "name": "Edit",
                          "input": {"file_path": "/w/scripts/x/bot.py"}}])
        self.assertEqual(dev_pipeline.classify_event(ev), "✏️ 編集 bot.py")

    def test_bash_shows_first_line(self):
        ev = self._asst([{"type": "tool_use", "name": "Bash",
                          "input": {"command": "python -m unittest\n他"}}])
        self.assertTrue(
            dev_pipeline.classify_event(ev).startswith("🔧 実行 python -m unittest"))

    def test_read_is_silent(self):
        ev = self._asst([{"type": "tool_use", "name": "Read",
                          "input": {"file_path": "x"}}])
        self.assertIsNone(dev_pipeline.classify_event(ev))

    def test_text_block_is_silent(self):
        self.assertIsNone(
            dev_pipeline.classify_event(self._asst([{"type": "text",
                                                     "text": "hi"}])))

    def test_result_is_silent(self):
        self.assertIsNone(
            dev_pipeline.classify_event({"type": "result", "result": "done"}))


class ProgressBufferTest(unittest.TestCase):
    def test_accumulates_and_renders(self):
        p = dev_pipeline.ProgressBuffer()
        p.set_phase("実装中")
        p.add_op("✏️ 編集 a.py")
        p.add_op("🔧 実行 x")
        phase, ops, last = p.snapshot()
        self.assertEqual((phase, ops, last), ("実装中", 2, "🔧 実行 x"))
        rendered = p.render()
        self.assertIn("実装中", rendered)
        self.assertIn("2操作", rendered)


class SummarizeTest(unittest.TestCase):
    CAP = {"id": 4, "description": "リマインダーの通知先を投稿先chにも指定可能に"}

    def test_success_marks_green_and_awaiting(self):
        s = dev_pipeline.summarize(
            self.CAP, test_ok=True, test_tail="", flakes_ok=True,
            flakes_tail="", diff_stat=" bot.py | 3 +--", final_text="bot.py変更",
            error=None)
        self.assertIn("起票#4", s)
        self.assertIn("承認待ち", s)
        self.assertIn("🟢", s)

    def test_failure_shows_reason(self):
        s = dev_pipeline.summarize(
            self.CAP, test_ok=False, test_tail="", flakes_ok=False,
            flakes_tail="", diff_stat="", final_text="", error="タイムアウト")
        self.assertIn("失敗", s)
        self.assertIn("タイムアウト", s)

    def test_red_test_appends_tail(self):
        s = dev_pipeline.summarize(
            self.CAP, test_ok=False, test_tail="FAILED (failures=1)",
            flakes_ok=True, flakes_tail="", diff_stat="x", final_text="",
            error=None)
        self.assertIn("赤", s)
        self.assertIn("FAILED", s)

    def test_salvaged_interrupt_becomes_approvable(self):
        # 中断エラーでも差分あり＋検証緑なら承認待ちサマリー（起票#7の反省）
        s = dev_pipeline.summarize(
            self.CAP, test_ok=True, test_tail="", flakes_ok=True,
            flakes_tail="", diff_stat=" bot.py | 3 +--", final_text="",
            error="応答が600秒無く中断しました", salvaged=True)
        self.assertIn("承認待ち", s)
        self.assertIn("⚠️", s)
        self.assertIn("600秒", s)
        self.assertNotIn("❌", s)

    def test_failure_mentions_resume(self):
        s = dev_pipeline.summarize(
            self.CAP, test_ok=False, test_tail="", flakes_ok=False,
            flakes_tail="", diff_stat="", final_text="", error="x")
        self.assertIn("続きから", s)


class SuitesForTest(unittest.TestCase):
    P = ""

    def test_archive_suite_always_runs(self):
        self.assertEqual(dev_pipeline.suites_for([]), ["core"])
        self.assertEqual(
            dev_pipeline.suites_for(["integrations/example_notes/__init__.py",
                                     "core/invoke_claude.py"]),
            ["core"])   # 連携のテストは core スイートが持つ

    def test_adds_touched_suites(self):
        got = dev_pipeline.suites_for(
            ["platforms/discord/dev/dev_pipeline.py",
             "platforms/discord/meeting/bot.py",
             "core/db.py"])
        # 常に回す chatbot が先頭、触られたスイートが名前順で続く
        self.assertEqual(got, ["core", "platforms"])

    def test_ignores_files_outside_scripts(self):
        got = dev_pipeline.suites_for(["README.md", "x.py"])
        self.assertEqual(got, ["core"])


class RestartTargetsTest(unittest.TestCase):
    P = ""

    def test_maps_dirs_to_processes(self):
        got = dev_pipeline.restart_targets(
            ["platforms/discord/bot.py",
             "integrations/example_notes/__init__.py",
             "platforms/discord/meeting/bot.py",
             "platforms/discord/dev/dev_pipeline.py"])
        self.assertEqual(got, ["archivebot", "devbot", "meetingbot"])

    def test_runner_is_imported_by_both_bots(self):
        # invoke_claude は archivebot と devbot の両プロセスに import されている
        self.assertEqual(
            dev_pipeline.restart_targets(["core/invoke_claude.py"]),
            ["archivebot", "devbot", "meetingbot"])

    def test_shared_db_module_restarts_devbot_too(self):
        # core/ は全BOTが自プロセスに import している（片方だけだと部分デプロイ）
        self.assertEqual(
            dev_pipeline.restart_targets(["core/db.py"]),
            ["archivebot", "devbot", "meetingbot"])

    def test_test_only_changes_need_no_restart(self):
        # テストはどのプロセスも import しない＝再起動不要（警告も出ない）
        self.assertEqual(
            dev_pipeline.restart_targets(
                ["platforms/discord/dev/test_dev_pipeline.py"]), [])

    def test_guidelines_only_needs_no_restart(self):
        # 規約はジョブごとに読み直すデータ＝再起動不要
        self.assertEqual(
            dev_pipeline.restart_targets(["platforms/discord/dev/dev-guidelines.md"]),
            [])

    def test_watchdog_and_outside_files_need_no_restart(self):
        self.assertEqual(
            dev_pipeline.restart_targets(
                ["dashboard/server/index.ts", "README.md"]), [])


class RiskWarningsTest(unittest.TestCase):
    P = ""

    def test_brain_change_warns_loudly(self):
        warns = dev_pipeline.risk_warnings(["platforms/discord/dev/bot.py"])
        self.assertEqual(len(warns), 1)
        self.assertIn("脳", warns[0])

    def test_guidelines_change_warns_softly(self):
        warns = dev_pipeline.risk_warnings(
            ["platforms/discord/dev/dev-guidelines.md"])
        self.assertEqual(len(warns), 1)
        self.assertIn("規約", warns[0])
        self.assertNotIn("脳", warns[0])

    def test_plain_change_has_no_warnings(self):
        # 会話BOTだけに効く変更＝他プロセスを巻き込まないので注意書き不要
        self.assertEqual(
            dev_pipeline.risk_warnings(
                ["platforms/discord/bot.py"]), [])

    def test_shared_module_change_notes_devbot_restart(self):
        # core/ は全BOTの土台なので、開発BOTと議事録BOTの両方に注意が要る
        warns = dev_pipeline.risk_warnings(["core/db.py"])
        self.assertIn("共有モジュール", warns[0])
        self.assertNotIn("脳", warns[0])
        self.assertTrue(any("録音中でないこと" in w for w in warns),
                        f"議事録BOTの再起動警告が無い: {warns}")

    def test_untested_dir_warns(self):
        warns = dev_pipeline.risk_warnings(
            ["dashboard/README.md"])
        self.assertTrue(any("テストスイートが" in w for w in warns))

    def test_deps_change_warns(self):
        warns = dev_pipeline.risk_warnings(
            ["requirements.txt"])
        self.assertTrue(any("依存" in w for w in warns))

    def test_meetingbot_restart_warns_about_recording(self):
        warns = dev_pipeline.risk_warnings(
            ["platforms/discord/meeting/bot.py"])
        self.assertTrue(any("録音中" in w for w in warns))

    def test_warnings_compose(self):
        warns = dev_pipeline.risk_warnings(
            ["platforms/discord/dev/bot.py",
             "platforms/discord/meeting/requirements.txt"])
        self.assertEqual(len(warns), 3)   # 脳＋依存＋録音

    def test_warnings_render_in_summary(self):
        s = dev_pipeline.summarize(
            {"id": 4, "description": "x"}, test_ok=True, test_tail="",
            flakes_ok=True, flakes_tail="", diff_stat="d", final_text="",
            warnings=["⚠️ 警告テキスト"])
        self.assertIn("⚠️ 警告テキスト", s)


class BuildPromptResumeTest(unittest.TestCase):
    CAP = {"id": 5, "description": "y", "context": None}

    def test_resume_adds_continue_section(self):
        p = dev_pipeline.build_prompt(self.CAP, resume=True)
        self.assertIn("前回からの続き", p)
        self.assertIn("git status", p)

    def test_default_has_no_continue_section(self):
        self.assertNotIn("前回からの続き", dev_pipeline.build_prompt(self.CAP))


class LessonsTest(unittest.TestCase):
    def test_format_lessons_block(self):
        block = dev_pipeline.format_lessons(
            [{"kind": "rejected", "text": "孤立ファイルを作った"},
             {"kind": "failed", "text": "タイムアウト"}])
        self.assertIn("教訓", block)
        self.assertIn("[却下] 孤立ファイルを作った", block)
        self.assertIn("[失敗] タイムアウト", block)

    def test_empty_or_blank_lessons_add_nothing(self):
        self.assertEqual(dev_pipeline.format_lessons(None), "")
        self.assertEqual(
            dev_pipeline.format_lessons([{"kind": "note", "text": " "}]), "")

    def test_build_prompt_includes_lessons(self):
        p = dev_pipeline.build_prompt(
            {"id": 1, "description": "x", "context": None},
            lessons=[{"kind": "failed", "text": "同名モジュール衝突"}])
        self.assertIn("同名モジュール衝突", p)

    def test_build_prompt_without_lessons_has_no_lesson_header(self):
        p = dev_pipeline.build_prompt(
            {"id": 1, "description": "x", "context": None})
        self.assertNotIn("過去の教訓", p)


class ClaudeArgvTest(unittest.TestCase):
    def test_partial_messages_keep_stream_alive(self):
        # 長考中の無音をidle検知が「ハング」と誤認しないための必須フラグ（起票#7の再発防止）
        argv = dev_pipeline.claude_argv("claude")
        self.assertIn("--include-partial-messages", argv)
        self.assertIn("stream-json", argv)

    def test_idle_timeout_tolerates_tool_silence(self):
        # partialが流れてもツール実行中は無音になるため、短すぎるidleは誤発動する
        self.assertGreaterEqual(dev_pipeline.IDLE_TIMEOUT_SEC, 600)

    def test_build_timeout_fits_opus_scale_work(self):
        # 起票#7は900秒の壁時計をほぼ使い切った。Opusの実装は30分見ておく
        self.assertGreaterEqual(dev_pipeline.BUILD_TIMEOUT_SEC, 1800)


class BuildPromptTest(unittest.TestCase):
    def test_contains_request_and_guardrails(self):
        p = dev_pipeline.build_prompt(
            {"id": 9, "description": "OCR機能", "context": "X投稿"})
        self.assertIn("#9", p)
        self.assertIn("OCR機能", p)
        self.assertIn("scripts/", p)
        self.assertIn("config.json", p)   # 触ってはいけない旨

    def test_injects_custom_guidelines(self):
        # 規約はデータ: 差し替えた本文がそのままプロンプトに入る
        p = dev_pipeline.build_prompt(
            {"id": 1, "description": "x", "context": None},
            guidelines="独自規約テキスト")
        self.assertIn("独自規約テキスト", p)
        self.assertNotIn("孤立した新規ファイル", p)   # フォールバックは使われない

    def test_load_guidelines_reads_shipped_file(self):
        text = dev_pipeline.load_guidelines()
        self.assertIn("統合", text)               # 同梱の規約ファイルが読める

    def test_load_guidelines_falls_back_when_missing(self):
        text = dev_pipeline.load_guidelines("/no/such/file.md")
        self.assertEqual(text, dev_pipeline.DEFAULT_GUIDELINES)


if __name__ == "__main__":
    unittest.main()
