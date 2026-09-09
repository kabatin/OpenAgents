#!/usr/bin/env python3
"""dev_pipeline の純粋関数テスト（claude/gitは起動しない）。

実行: ../chatbot/venv/bin/python -m unittest test_dev_pipeline -v
"""

import os
import unittest

from core import paths
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
        # 会話BOTだけに効く変更（中核以外）＝他プロセスを巻き込まないので注意書き不要
        self.assertEqual(
            dev_pipeline.risk_warnings(
                ["platforms/discord/reaction_handlers.py"]), [])

    def test_shared_module_change_notes_devbot_restart(self):
        # core/ は全BOTの土台なので、開発BOTと議事録BOTの両方に注意が要る
        warns = dev_pipeline.risk_warnings(["core/summaries.py"])
        self.assertIn("共有モジュール", warns[0])
        self.assertNotIn("脳", warns[0])
        self.assertTrue(any("録音中でないこと" in w for w in warns),
                        f"議事録BOTの再起動警告が無い: {warns}")

    def test_core_brain_file_warns_first_then_restart(self):
        # 中核（db.py）は 🧠 警告が先頭、共有モジュールの再起動注意も残る
        warns = dev_pipeline.risk_warnings(["core/db.py"])
        self.assertIn("中核", warns[0])
        self.assertTrue(any("共有モジュール" in w for w in warns))

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


class Phase5Test(unittest.TestCase):
    def test_brain_files_warning(self):
        warns = dev_pipeline.risk_warnings(
            ["platforms/discord/bot.py", "core/x.py"])
        self.assertTrue(any("中核" in w and "bot.py" in w for w in warns))
        self.assertEqual(dev_pipeline.brain_files_touched(
            ["core/tasks.py"]), [])
        self.assertEqual(dev_pipeline.brain_files_touched(
            ["core/invoke_claude.py"]), ["invoke_claude.py"])
        self.assertEqual(dev_pipeline.brain_files_touched(
            ["platforms\\discord\\agent_loops.py"]), ["agent_loops.py"])

    def test_no_dialect_in_warnings(self):
        warns = dev_pipeline.risk_warnings(
            ["platforms/discord/dev/bot.py", "requirements.txt",
             "platforms/discord/meeting/bot.py", "builder/x.py"])
        self.assertTrue(warns)
        for w in warns:
            self.assertNotIn("っす", w)

    def test_job_log_writes_events_and_finish(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "jobs", "1.log")
            log = dev_pipeline.JobLog(path)
            log.event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
                {"type": "text", "text": "直します"}]}})
            log.event({"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": "denied"}]}})
            log.event({"type": "result", "subtype": "success", "num_turns": 3,
                       "total_cost_usd": 0.5, "result": "done"})
            log.finish({"error": "exit=1: boom", "stderr": "traceback…"})
            with open(path, encoding="utf-8") as f:
                text = f.read()
        for needle in ("TOOL Edit", "TEXT 直します", "TOOL_ERROR denied",
                       "RESULT success", "ERROR exit=1", "STDERR traceback"):
            self.assertIn(needle, text)

    def test_job_log_path_under_state_and_label(self):
        p = dev_pipeline.job_log_path(7)
        self.assertTrue(p.endswith(os.path.join("dev-jobs", "7.log")))
        self.assertTrue(p.startswith(paths.STATE_DIR))
        self.assertIn("dev-jobs/7.log", dev_pipeline.job_log_label(7))


class VerifyPassTest(unittest.TestCase):
    def test_prompt_parse_warning(self):
        cap = {"id": 19, "description": "通知先を個別に設定できるようにする"}
        p = dev_pipeline.verify_prompt(cap, "+ x = 1", "OK")
        self.assertIn("起票 #19", p)
        self.assertIn("+ x = 1", p)
        self.assertEqual(dev_pipeline.parse_verify('{"verdict": "pass", "notes": []}'),
                         {"verdict": "pass", "notes": []})
        r = dev_pipeline.parse_verify(
            '前置き {"verdict": "concern", "notes": ["テスト不足", 2]} 後')
        self.assertEqual(r["verdict"], "concern")
        self.assertEqual(r["notes"], ["テスト不足", "2"])
        self.assertIsNone(dev_pipeline.parse_verify('{"verdict": "maybe"}'))
        self.assertIsNone(dev_pipeline.parse_verify("JSONなし"))
        self.assertIn("問題なし",
                      dev_pipeline.verify_warning({"verdict": "pass", "notes": []}))
        w = dev_pipeline.verify_warning({"verdict": "concern", "notes": ["a"]})
        self.assertIn("気になる点あり", w)
        self.assertIn("　・a", w)
        self.assertIn("実行できませんでした", dev_pipeline.verify_warning(None))

    def test_verify_uses_injected_fn_and_skips_empty_diff(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.email=t@example.com",
                            "-c", "user.name=t", "commit", "-q",
                            "--allow-empty", "-m", "init"], check=True)
            self.assertIsNone(dev_pipeline.verify(
                {"id": 1, "description": "x"}, d,
                invoke_fn=lambda p: '{"verdict":"pass"}'))
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("x = 1\n")
            subprocess.run(["git", "-C", d, "add", "a.py"], check=True)
            seen = {}

            def fake(p):
                seen["prompt"] = p
                return '{"verdict": "concern", "notes": ["テストが無い"]}'
            r = dev_pipeline.verify({"id": 1, "description": "x"}, d,
                                    invoke_fn=fake)
            self.assertEqual(r["verdict"], "concern")
            self.assertIn("x = 1", seen["prompt"])

            # 検証側の例外は None（実装を捨てない）
            def boom(p):
                raise RuntimeError("claude down")
            self.assertIsNone(dev_pipeline.verify(
                {"id": 1, "description": "x"}, d, invoke_fn=boom))


if __name__ == "__main__":
    unittest.main()


class PermissionSafetyTest(unittest.TestCase):
    """ホーム配下の deny は秘密の場所だけを名指しする。
    丸ごと拒否すると作業ツリーごと読めなくなり、macOS の TCC 対象を書くと
    画面を出せない常駐プロセスが許可待ちで永久に固まる。"""

    def _deny(self):
        import json as _json
        return _json.loads(dev_pipeline.dev_settings())["permissions"]["deny"]

    def test_home_wide_read_deny_removed(self):
        self.assertNotIn("Read(~/**)", self._deny())
        self.assertIn("Read(~/.ssh/**)", self._deny())

    def test_no_tcc_paths_in_deny(self):
        deny = " ".join(self._deny()).lower()
        for p in dev_pipeline.TCC_PATHS:
            self.assertNotIn(p.lstrip("~/"), deny)

    def test_tcc_paths_are_denied_in_bash_instead(self):
        from platforms.discord.dev import dev_gate
        for p in dev_pipeline.TCC_PATHS:
            self.assertIn(p, dev_gate.BASH_SECRET_HINTS)
        for name in (".ssh", "id_rsa", ".aws", ".gnupg"):
            self.assertIn(name, dev_gate.BASH_SECRET_HINTS)

    def test_argv_ignores_repo_settings(self):
        argv = dev_pipeline.claude_argv("/bin/claude")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")


class IdleDiagnosticsTest(unittest.TestCase):
    def test_idle_error_reports_event_count(self):
        import sys
        import time as _t
        # 何も出力せず眠る子プロセス → 無音タイムアウトの文言を検査
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        orig = dev_pipeline.claude_argv
        dev_pipeline.claude_argv = lambda *a, **k: argv
        try:
            t0 = _t.monotonic()
            run = dev_pipeline.stream_claude("x", None, lambda ev: None,
                                             timeout=20, idle_timeout=2)
        finally:
            dev_pipeline.claude_argv = orig
        self.assertLess(_t.monotonic() - t0, 15)
        self.assertIn("受信イベント0件", run["error"])
        self.assertIn("最後はなし", run["error"])
        self.assertIn("子プロセス", run["error"])
