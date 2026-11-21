#!/usr/bin/env python3
"""本番のツール方式の回答に、現在日時が届くことのテスト。

実行: ./venv/bin/python -m unittest platforms.discord.test_now_note
"""

import os
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

from core import db
from core.archive_tools import launch
from platforms.discord import bot

NOW = datetime(2026, 11, 10, 19, 50)


class NowNoteTest(unittest.TestCase):

    def test_has_date_weekday_and_time(self):
        self.assertIn("2026-11-10(火) 19:50", launch.now_note(NOW))


class BotClockTest(unittest.TestCase):
    """本番の回答に現在日時が届くか。ツール方式ではリマインダーの指示文
    （現在日時つき）を出さないので、日時が無いと「30分後」を登録できない。"""

    def _param(self, shadow):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        db.init_db(path)
        off = dict.fromkeys(
            ("image_gen", "is_archiver", "youtube_summary", "pdf_summary",
             "reminder", "action_tracking"), False)
        dummy = types.SimpleNamespace(
            agent={"id": "agent1", "name": "エージェント", "role": ""},
            persona_files=[], runner_enabled=True,
            tool_loop_cfg={"enabled": True, "shadow": shadow},
            integrations=[], _bg_available=lambda: False,
            _active_rules=lambda m: [],
            _tool_loop_on=lambda: True, _tool_loop_live=lambda: not shadow,
            _integration_ctx=lambda m: None, **off)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, id=1),
            channel=types.SimpleNamespace(id=5))
        with mock.patch.object(bot, "DB_PATH", path), \
                mock.patch.object(bot.reminders, "now_jst", return_value=NOW):
            return bot.AgentClient._build_agent_param(dummy, message)

    def test_live_tool_turn_gets_the_clock(self):
        param = self._param(shadow=False)
        self.assertIn("現在日時: 2026-11-10(火) 19:50", param["context"])
        # 分ごとに変わるので、キャッシュされる system 側（role）には入れない
        self.assertNotIn("現在日時", param["role"])

    def test_shadow_turn_is_unchanged(self):
        self.assertNotIn("現在日時", self._param(shadow=True)["context"])


if __name__ == "__main__":
    unittest.main()
