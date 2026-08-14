#!/usr/bin/env python3
"""会話セッション持続（resume方式カナリア）のユニットテスト。"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

from core import db
from core import runner_answer
from core import sessions

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
from core import invoke_claude
NOW = datetime(2026, 8, 3, 12, 0)
CFG = {"hot_minutes": 60, "max_turns": 20, "max_age_hours": 6}


def _row(last="2026-08-03T11:30", started="2026-08-03T10:00", turns=3,
         sid="abc"):
    return {"session_id": sid, "turns": turns, "started_at": started,
            "last_used_at": last}


class ShouldResumeTest(unittest.TestCase):
    def test_hot_window_resumes(self):
        self.assertTrue(sessions.should_resume(_row(), CFG, now=NOW))

    def test_cold_conversation_starts_fresh(self):
        self.assertFalse(sessions.should_resume(
            _row(last="2026-08-03T10:30"), CFG, now=NOW))   # 90分前

    def test_turn_cap_rotates(self):
        self.assertFalse(sessions.should_resume(
            _row(turns=20), CFG, now=NOW))

    def test_age_cap_rotates(self):
        self.assertFalse(sessions.should_resume(
            _row(started="2026-08-03T05:00", last="2026-08-03T11:59"),
            CFG, now=NOW))                                   # 7時間前開始

    def test_missing_row(self):
        self.assertFalse(sessions.should_resume(None, CFG, now=NOW))
        self.assertFalse(sessions.should_resume(
            {"session_id": None}, CFG, now=NOW))
        self.assertFalse(sessions.should_resume(
            _row(last=None), CFG, now=NOW))


class RecordUseTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _get(self):
        with db.connect(self.db_path) as conn:
            return db.get_agent_session(conn, "agent1", 7)

    def test_new_generation_then_increment(self):
        sessions.record_use(self.db_path, "agent1", 7, "s1", resumed=False,
                            now=NOW)
        self.assertEqual(self._get()["turns"], 1)
        sessions.record_use(self.db_path, "agent1", 7, "s1", resumed=True,
                            now=NOW)
        row = self._get()
        self.assertEqual(row["turns"], 2)
        self.assertEqual(row["started_at"], row["last_used_at"])

    def test_rotation_resets_turns(self):
        sessions.record_use(self.db_path, "agent1", 7, "s1", resumed=False,
                            now=NOW)
        sessions.record_use(self.db_path, "agent1", 7, "s2", resumed=False,
                            now=NOW)
        row = self._get()
        self.assertEqual(row["session_id"], "s2")
        self.assertEqual(row["turns"], 1)

    def test_resume_id_and_clear(self):
        sessions.record_use(self.db_path, "agent1", 7, "s1", resumed=False,
                            now=NOW)
        self.assertEqual(
            sessions.resume_id(self.db_path, "agent1", 7, CFG, now=NOW), "s1")
        sessions.clear(self.db_path, "agent1", 7)
        self.assertIsNone(
            sessions.resume_id(self.db_path, "agent1", 7, CFG, now=NOW))

    def test_empty_session_id_not_recorded(self):
        sessions.record_use(self.db_path, "agent1", 7, None, resumed=False,
                            now=NOW)
        self.assertIsNone(self._get())


class BuildArgvTest(unittest.TestCase):
    def test_resume_flag_composed(self):
        argv = invoke_claude.build_argv("claude", model="m", resume="sid-1")
        i = argv.index("--resume")
        self.assertEqual(argv[i + 1], "sid-1")
        self.assertIn("--tools", argv)      # 既定はツール無効のまま

    def test_no_resume_by_default(self):
        argv = invoke_claude.build_argv("claude", model="m")
        self.assertNotIn("--resume", argv)

    def test_extract_session_id(self):
        events = [{"type": "system"}, {"type": "result",
                                       "session_id": "xyz"}]
        self.assertEqual(invoke_claude.extract_session_id(events), "xyz")
        self.assertIsNone(invoke_claude.extract_session_id([{}]))


class FakeInvokeResult:
    def __init__(self, text, session_id):
        self.text = text
        self.session_id = session_id


class RunnerPlumbingTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        self.calls = []
        self._orig = runner_answer.invoke_claude.invoke

        def fake(prompt, **kwargs):
            self.calls.append(kwargs)
            return FakeInvokeResult("答えっス", "sess-9")
        runner_answer.invoke_claude.invoke = fake

    def tearDown(self):
        runner_answer.invoke_claude.invoke = self._orig
        os.unlink(self.db_path)

    def test_resume_and_cwd_passed_and_session_returned(self):
        result = runner_answer.answer_question(
            self.db_path, "1", "", agent={"name": "アーカイブ担当",
                                          "persona_files": [], "role": ""},
            resume="prev-sid", session_cwd="/fixed/cwd")
        self.assertEqual(result["session_id"], "sess-9")
        last = self.calls[-1]
        self.assertEqual(last.get("resume"), "prev-sid")
        self.assertEqual(last.get("cwd"), "/fixed/cwd")

    def test_without_session_no_resume_kwarg(self):
        result = runner_answer.answer_question(
            self.db_path, "1", "", agent={"name": "アーカイブ担当",
                                          "persona_files": [], "role": ""})
        self.assertEqual(result["session_id"], "sess-9")
        self.assertNotIn("resume", self.calls[-1])
        self.assertNotIn("cwd", self.calls[-1])


if __name__ == "__main__":
    unittest.main()
