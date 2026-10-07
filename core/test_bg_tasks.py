#!/usr/bin/env python3
"""重い作業は裏で進めて、途中経過と結果をスレッドに置く（core/bg_tasks.py）。"""

import os
import tempfile
import unittest

from core import bg_tasks
from core import db


class MarkerTest(unittest.TestCase):
    def test_marker_is_taken_from_the_end(self):
        text, instr = bg_tasks.extract_marker(
            "承知しました、取りかかります。\n"
            "[TASK: 会場候補3つを価格・アクセス・定員で比較した表をMarkdownで作る]")
        self.assertEqual(text, "承知しました、取りかかります。")
        self.assertEqual(instr, "会場候補3つを価格・アクセス・定員で比較した表をMarkdownで作る")

    def test_no_marker(self):
        self.assertEqual(bg_tasks.extract_marker("ただの返事です"), ("ただの返事です", None))

    def test_empty_marker_is_dropped(self):
        self.assertEqual(bg_tasks.extract_marker("やります [TASK: ]"), ("やります", None))

    def test_skill_note_has_no_dialect(self):
        self.assertIn("[TASK:", bg_tasks.SKILL_NOTE)
        self.assertNotIn("っス", bg_tasks.SKILL_NOTE + bg_tasks.WORKER_PROMPT)


class ConfigTest(unittest.TestCase):
    def test_off_unless_enabled(self):
        self.assertIsNone(bg_tasks.normalize(None))
        self.assertIsNone(bg_tasks.normalize({"enabled": False}))
        self.assertIsNone(bg_tasks.normalize("yes"))
        self.assertEqual(bg_tasks.normalize(True)["max_parallel"], bg_tasks.MAX_PARALLEL)

    def test_limits_are_clamped(self):
        cfg = bg_tasks.normalize({"enabled": True, "max_parallel": 99,
                                  "timeout_min": 1, "report_interval_min": "x"})
        self.assertEqual(cfg["max_parallel"], 5)
        self.assertEqual(cfg["timeout_min"], 5)
        self.assertEqual(cfg["report_interval_min"], bg_tasks.REPORT_INTERVAL_SEC // 60)


class ProgressTest(unittest.TestCase):
    def _ev(self, *names):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": n} for n in names]}}

    def test_tool_names_become_words(self):
        steps = []
        for ev in (self._ev("mcp__archive__search_messages"), self._ev("WebSearch"),
                   self._ev("WebSearch"), self._ev("Write")):
            steps += bg_tasks.steps_from_event(ev)
        self.assertEqual(bg_tasks.label_steps(steps),
                         ["社内ログを調べる", "Webで調べる", "ファイルを書く"])

    def test_progress_text(self):
        t = bg_tasks.progress_text(360, ["社内ログを調べる", "Webで調べる", "ファイルを書く"])
        self.assertEqual(t, "⏳ 6分経過: 社内ログを調べる → Webで調べる → ファイルを書く …")
        self.assertEqual(bg_tasks.progress_text(60, []), "⏳ 1分経過: 考え中…")

    def test_reports_every_interval_only_when_changed(self):
        self.assertFalse(bg_tasks.due_report(now=100, last_at=0, interval=180, changed=True))
        self.assertTrue(bg_tasks.due_report(now=181, last_at=0, interval=180, changed=True))
        self.assertFalse(bg_tasks.due_report(now=400, last_at=181, interval=180, changed=False))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_lifecycle_and_restart_interrupts_running(self):
        tid = bg_tasks.create(self.db_path, agent_id="agent1", channel_id=1, thread_id=2,
                              requester_id="9", instruction="表を作る")
        self.assertEqual(bg_tasks.running_count(self.db_path, "agent1"), 1)
        stale = bg_tasks.interrupt_running(self.db_path, "agent1")
        self.assertEqual([t["id"] for t in stale], [tid])
        self.assertEqual(bg_tasks.get(self.db_path, tid)["status"], "interrupted")
        tid2 = bg_tasks.create(self.db_path, agent_id="agent1", channel_id=1, thread_id=2,
                               requester_id="9", instruction="x")
        bg_tasks.finish(self.db_path, tid2, "done", "できた")
        row = bg_tasks.get(self.db_path, tid2)
        self.assertEqual((row["status"], row["summary"]), ("done", "できた"))
        self.assertEqual(bg_tasks.running_count(self.db_path, "agent1"), 0)


class SandboxTest(unittest.TestCase):
    def test_write_is_allowed_only_inside_the_workdir(self):
        allow = bg_tasks.allow_rules("/tmp/bg-1", ("mcp__archive__search_messages",))
        root = bg_tasks.rule_path(os.path.realpath("/tmp/bg-1"))
        self.assertIn(f"Write({root}/**)", allow)
        self.assertIn(f"Edit({root}/**)", allow)
        self.assertTrue(root.startswith("//"))          # 絶対パスの印
        self.assertNotIn("Write", allow)               # 範囲なしの書き込み許可は出さない
        self.assertIn("mcp__archive__search_messages", allow)
        self.assertNotIn("Bash", bg_tasks.TOOLS)

    def test_windows_paths_become_rule_paths(self):
        # Claude Code の許可ルールは POSIX 形式（C:\\x → //c/x）
        self.assertEqual(bg_tasks.rule_path("C:\\Users\\me\\AppData\\Local\\Temp\\bg-1"),
                         "//c/Users/me/AppData/Local/Temp/bg-1")
        self.assertEqual(bg_tasks.rule_path("/private/var/folders/x/bg-1/"),
                         "//private/var/folders/x/bg-1")

    def test_new_files_are_collected_within_limit(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "表.md"), "w", encoding="utf-8") as f:
            f.write("| a |")
        with open(os.path.join(d, "big.bin"), "wb") as f:
            f.write(b"x" * 2000)
        with open(os.path.join(d, ".hidden"), "w", encoding="utf-8") as f:
            f.write("x")
        files, skipped = bg_tasks.output_files(d, limit_bytes=1000)
        self.assertEqual([os.path.basename(f) for f in files], ["表.md"])
        self.assertEqual(skipped, ["big.bin"])


if __name__ == "__main__":
    unittest.main()
