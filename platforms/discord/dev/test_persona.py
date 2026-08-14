#!/usr/bin/env python3
"""つむぎ(開発BOT)の口調メッセージのテスト。"""

import unittest

from platforms.discord.dev import monitor
from platforms.discord.dev import persona


class PersonaTest(unittest.TestCase):
    def test_startup_mentions_targets_and_interval(self):
        s = persona.startup("archivebot, meetingbot", 60)
        self.assertIn("archivebot", s)
        self.assertIn("60", s)

    def test_alert_covers_all_statuses_and_names(self):
        for st in (monitor.DOWN, monitor.DISCONNECTED, monitor.STALLED,
                   monitor.OK):
            msg = persona.alert("archivebot", st)
            self.assertIn("archivebot", msg)
            self.assertTrue(msg.strip())

    def test_down_and_recover_are_distinct(self):
        self.assertNotEqual(persona.alert("x", monitor.DOWN),
                            persona.alert("x", monitor.OK))

    def test_job_messages_include_req_id(self):
        self.assertIn("4", persona.job_start(4, "説明"))
        self.assertIn("4", persona.job_finished(4, True))
        self.assertIn("4", persona.job_finished(4, False))
        self.assertIn("4", persona.job_error(4, "err"))
        self.assertIn("4", persona.job_not_found(4))
        self.assertIn("4", persona.already_running(4))
        self.assertIn("4", persona.job_interrupted(4))


class SanitizeReplyTest(unittest.TestCase):
    """会話返信のツールコール漏れ検知（本番事故の再発防止）。"""

    def setUp(self):
        from platforms.discord.dev import bot
        self.sanitize = bot._sanitize_reply

    def test_passes_normal_reply(self):
        self.assertEqual(self.sanitize("元気っすよ〜🧵"), "元気っすよ〜🧵")

    def test_blocks_bash_tool_leak(self):
        leak = 'Bash: search for reminder\n{\n  "command": "grep -rn x"\n}'
        self.assertNotIn("command", self.sanitize(leak))
        self.assertIn("!status", self.sanitize(leak))

    def test_blocks_empty(self):
        self.assertIn("!status", self.sanitize(""))

    def test_blocks_command_json(self):
        self.assertIn("!status", self.sanitize('{"command": "ls"}'))

    def test_progress_shows_op_count(self):
        self.assertIn("3", persona.progress("実装", 3, "✏️ 編集 a.py"))

    def test_fixed_lines_nonempty(self):
        for s in (persona.ping(), persona.status_header(),
                  persona.not_admin(), persona.job_phase_test(),
                  persona.restarting()):
            self.assertTrue(s.strip())

    def test_phase3_messages_include_req_id(self):
        self.assertIn("4", persona.rejected(4))
        self.assertIn("4", persona.approving(4))
        self.assertIn("4", persona.deployed(4))
        self.assertIn("4", persona.deploy_failed(4, "理由"))
        self.assertIn("4", persona.deploy_rolled_back(4))


if __name__ == "__main__":
    unittest.main()
