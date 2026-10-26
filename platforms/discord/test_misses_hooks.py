#!/usr/bin/env python3
"""返信で受け取る失敗の台帳への入口（❌の理由・直し方の見本）のテスト。"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as NS

from core import db
from core import misses
from core import reminders
from platforms.discord import misses_hooks


class _Msg:
    def __init__(self, content, ref_id, author_id=500, mentions=()):
        self.content = content
        self.clean_content = content
        self.reference = NS(message_id=ref_id) if ref_id else None
        self.author = NS(id=author_id, bot=False)
        self.raw_mentions = list(mentions)
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class MissesHooksTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        self._orig = misses_hooks.DB_PATH
        misses_hooks.DB_PATH = self.db_path
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_state = reminders.STATE_FILE
        reminders.STATE_FILE = os.path.join(self.tmp.name, "reminders.json")
        self.me = NS(user=NS(id=999), is_archiver=True, agent={"id": "agent1"})

    def tearDown(self):
        misses_hooks.DB_PATH = self._orig
        reminders.STATE_FILE = self._orig_state
        self.tmp.cleanup()
        os.unlink(self.db_path)

    def _reason(self, msg):
        return asyncio.run(misses_hooks.MissesHooksMixin._maybe_miss_reason(self.me, msg))

    def test_reply_to_rejected_proposal_is_stored_with_ack(self):
        misses.record_rejection(self.db_path, agent_id="agent1", source="ripple",
                                ref_message_id=100, context="決定Aの波及案")
        msg = _Msg("その日は祝日です", 100)
        self.assertTrue(self._reason(msg))
        self.assertEqual(msg.reactions, ["📝"])          # 会話にせず受け取ったことだけ返す
        self.assertEqual(misses.recent(self.db_path)[0]["reason"], "その日は祝日です")

    def test_mention_or_unrelated_reply_is_a_normal_message(self):
        misses.record_rejection(self.db_path, agent_id="agent1", source="ripple",
                                ref_message_id=100, context="決定Aの波及案")
        self.assertFalse(self._reason(_Msg("お願いします", 100, mentions=[999])))
        self.assertFalse(self._reason(_Msg("こんにちは", 555)))
        self.assertFalse(self._reason(_Msg("返信ではない", None)))

    def test_teaching_records_changes_only_when_something_changed(self):
        with db.connect(self.db_path) as conn:
            db.add_action_item(conn, agent_id="agent1", source_message_id=1,
                               channel_id=7, task="定例の準備", owners="<@1>",
                               due_date="2026-10-15", urgent=0, created_at="t")
        msg = _Msg("10/16から木曜で", 100)
        before = misses.snapshot(self.db_path)
        # 変化なし → 何も残さない
        asyncio.run(misses_hooks.MissesHooksMixin._record_teaching(
            self.me, msg, "決定「定例は木曜」", before))
        self.assertEqual(misses.recent(self.db_path), [])
        with db.connect(self.db_path) as conn:
            conn.execute("UPDATE action_items SET due_date='2026-10-16'")
        asyncio.run(misses_hooks.MissesHooksMixin._record_teaching(
            self.me, msg, "決定「定例は木曜」", before))
        rows = misses.recent(self.db_path)
        self.assertEqual(rows[0]["source"], "taught")
        self.assertEqual(rows[0]["reason"], "10/16から木曜で")
        self.assertIn("2026-10-15 → 2026-10-16", rows[0]["detail"])


class CorrectionHookTest(unittest.TestCase):
    """エージェントの投稿への返信が訂正・不満なら台帳に残す（会話は止めない）。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        self._orig = misses_hooks.DB_PATH
        misses_hooks.DB_PATH = self.db_path
        self._orig_ids = dict(misses_hooks.AGENT_USER_IDS)
        misses_hooks.AGENT_USER_IDS.clear()
        misses_hooks.AGENT_USER_IDS["agent1"] = 999
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text")
            db.upsert_user(conn, id=999, name="bot", display_name="エージェント",
                           is_bot=True)
            db.upsert_user(conn, id=500, name="u", display_name="人A", is_bot=False)
            db.insert_message(conn, id=10, channel_id=1, author_id=999,
                              content="締め切りは明日です",
                              created_at="2026-10-16T00:00:00+00:00")
            db.insert_message(conn, id=11, channel_id=1, author_id=500,
                              content="人の発言", created_at="2026-10-16T00:00:00+00:00")
        self.me = NS(user=NS(id=999), is_archiver=True, agent={"id": "agent1"})

    def tearDown(self):
        misses_hooks.DB_PATH = self._orig
        misses_hooks.AGENT_USER_IDS.clear()
        misses_hooks.AGENT_USER_IDS.update(self._orig_ids)
        os.unlink(self.db_path)

    def _run(self, msg):
        msg.author.display_name = "人A"
        asyncio.run(misses_hooks.MissesHooksMixin._maybe_record_correction(self.me, msg))
        return misses.recent(self.db_path)

    def test_correction_to_agent_post_is_recorded(self):
        rows = self._run(_Msg("違う、締め切りは来週", 10))
        self.assertEqual([r["source"] for r in rows], ["corrected"])
        self.assertIn("締め切りは明日です", rows[0]["context"])
        self.assertIn("違う、締め切りは来週", rows[0]["detail"])

    def test_ordinary_reply_or_human_post_is_not_recorded(self):
        self.assertEqual(self._run(_Msg("ありがとう", 10)), [])
        self.assertEqual(self._run(_Msg("違うよ", 11)), [])   # 人の投稿への返信
        self.assertEqual(self._run(_Msg("違うよ", None)), [])  # 返信ではない


if __name__ == "__main__":
    unittest.main()
