#!/usr/bin/env python3
"""スレッド返信（thread_reply + agent_runtime.deliver_reply）のユニットテスト。

純粋ロジック（正規化・発火判定・スレッド名）と、投稿先の切り替えIO
（deliver_reply / _open_thread / _log_shadow）を fake discord オブジェクトで
検証する。実DiscordにもDBにも触れない。"""

import asyncio
import unittest
from types import SimpleNamespace

from core import thread_reply
from platforms.discord import agent_runtime


# ---- fake discord オブジェクト -------------------------------------------

class FakeChannel:
    def __init__(self, cid=1):
        self.id = cid
        self.sent = []

    async def send(self, content, **kw):
        self.sent.append(content)
        return SimpleNamespace(id=999)


class FakeMessage:
    def __init__(self, *, content="要件を教えて", author_bot=False,
                 channel=None, thread=None, mid=100, raise_on_thread=False):
        self.clean_content = content
        self.author = SimpleNamespace(bot=author_bot)
        self.channel = channel or FakeChannel()
        self.thread = thread
        self.id = mid
        self._raise = raise_on_thread
        self.created_name = None

    async def create_thread(self, *, name):
        if self._raise:
            raise RuntimeError("no permission")
        self.created_name = name
        return FakeChannel(cid=555)


def run(coro):
    return asyncio.run(coro)


# ---- 純粋ロジック ---------------------------------------------------------

class NormalizeTest(unittest.TestCase):
    def test_default_off(self):
        cfg = thread_reply.normalize(None)
        self.assertFalse(cfg["enabled"])
        self.assertTrue(cfg["shadow"])
        self.assertEqual(cfg["flows"], thread_reply.DEFAULT_FLOWS)

    def test_empty_dict_off(self):
        self.assertFalse(thread_reply.normalize({})["enabled"])

    def test_enabled_and_custom_flows(self):
        cfg = thread_reply.normalize(
            {"enabled": True, "shadow": False, "flows": ["pdf"]})
        self.assertTrue(cfg["enabled"])
        self.assertFalse(cfg["shadow"])
        self.assertEqual(cfg["flows"], ("pdf",))

    def test_shadow_defaults_true_when_enabled(self):
        self.assertTrue(thread_reply.normalize({"enabled": True})["shadow"])


class WantsTest(unittest.TestCase):
    def setUp(self):
        self.on = thread_reply.normalize({"enabled": True})

    def test_enabled_human_in_flow(self):
        self.assertTrue(thread_reply.wants(self.on, "mention"))
        self.assertTrue(thread_reply.wants(self.on, "pdf"))

    def test_bot_never(self):
        self.assertFalse(thread_reply.wants(self.on, "mention", is_bot=True))

    def test_disabled_never(self):
        off = thread_reply.normalize(None)
        self.assertFalse(thread_reply.wants(off, "mention"))

    def test_kind_out_of_flow(self):
        cfg = thread_reply.normalize({"enabled": True, "flows": ["pdf"]})
        self.assertFalse(thread_reply.wants(cfg, "mention"))
        self.assertTrue(thread_reply.wants(cfg, "pdf"))


class ThreadNameTest(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(
            thread_reply.thread_name("  A\n\nB  ", kind="mention"), "A B")

    def test_empty_falls_back_per_kind(self):
        self.assertEqual(thread_reply.thread_name("", kind="pdf"), "PDF要約")
        self.assertEqual(
            thread_reply.thread_name("   ", kind="mention"), "スレッド返信")

    def test_truncates_to_limit_with_ellipsis(self):
        n = thread_reply.thread_name("あ" * 250, kind="mention")
        self.assertEqual(len(n), thread_reply.THREAD_NAME_MAX)
        self.assertTrue(n.endswith("…"))

    def test_exactly_at_limit_no_ellipsis(self):
        n = thread_reply.thread_name("あ" * thread_reply.THREAD_NAME_MAX,
                                     kind="mention")
        self.assertEqual(len(n), thread_reply.THREAD_NAME_MAX)
        self.assertFalse(n.endswith("…"))


# ---- deliver_reply（投稿先の切り替えIO） --------------------------------

class DeliverReplyTest(unittest.TestCase):
    def setUp(self):
        # _log_shadow を差し替えてDB書込を避けつつ、呼び出しを記録する
        self._orig_log = agent_runtime._log_shadow
        self.shadow_calls = []

        def fake_log(agent_id, message, kind, name):
            self.shadow_calls.append((agent_id, kind, name))
        agent_runtime._log_shadow = fake_log

    def tearDown(self):
        agent_runtime._log_shadow = self._orig_log

    def test_disabled_sends_to_channel(self):
        cfg = thread_reply.normalize(None)
        msg = FakeMessage()
        target = run(agent_runtime.deliver_reply(
            msg, "本文", cfg=cfg, kind="mention", agent_id="agent1"))
        self.assertIs(target, msg.channel)
        self.assertEqual(msg.channel.sent, ["本文"])
        self.assertIsNone(msg.created_name)
        self.assertEqual(self.shadow_calls, [])

    def test_bot_author_sends_to_channel(self):
        cfg = thread_reply.normalize({"enabled": True, "shadow": False})
        msg = FakeMessage(author_bot=True)
        target = run(agent_runtime.deliver_reply(
            msg, "本文", cfg=cfg, kind="mention", agent_id="agent1"))
        self.assertIs(target, msg.channel)
        self.assertIsNone(msg.created_name)

    def test_shadow_logs_and_sends_to_channel(self):
        cfg = thread_reply.normalize({"enabled": True})  # shadow 既定 True
        msg = FakeMessage(content="議事録を要約して")
        target = run(agent_runtime.deliver_reply(
            msg, "本文", cfg=cfg, kind="mention", agent_id="agent1"))
        self.assertIs(target, msg.channel)
        self.assertEqual(msg.channel.sent, ["本文"])
        self.assertIsNone(msg.created_name)                 # 実スレッドは作らない
        self.assertEqual(len(self.shadow_calls), 1)
        self.assertEqual(self.shadow_calls[0][1], "mention")
        self.assertEqual(self.shadow_calls[0][2], "議事録を要約して")

    def test_real_creates_thread_and_sends_there(self):
        cfg = thread_reply.normalize({"enabled": True, "shadow": False})
        msg = FakeMessage(content="設計方針を相談したい")
        target = run(agent_runtime.deliver_reply(
            msg, "本文", cfg=cfg, kind="mention", agent_id="agent1"))
        self.assertIsNot(target, msg.channel)               # スレッドへ
        self.assertEqual(msg.created_name, "設計方針を相談したい")
        self.assertEqual(target.sent, ["本文"])
        self.assertEqual(msg.channel.sent, [])              # 元chには出さない
        self.assertEqual(self.shadow_calls, [])

    def test_real_reuses_existing_thread(self):
        cfg = thread_reply.normalize({"enabled": True, "shadow": False})
        existing = FakeChannel(cid=777)
        msg = FakeMessage(thread=existing)
        target = run(agent_runtime.deliver_reply(
            msg, "本文", cfg=cfg, kind="pdf", agent_id="agent1"))
        self.assertIs(target, existing)
        self.assertEqual(existing.sent, ["本文"])
        self.assertIsNone(msg.created_name)                 # 新規作成しない

    def test_thread_creation_failure_falls_back_to_channel(self):
        cfg = thread_reply.normalize({"enabled": True, "shadow": False})
        msg = FakeMessage(raise_on_thread=True)
        target = run(agent_runtime.deliver_reply(
            msg, "本文", cfg=cfg, kind="mention", agent_id="agent1"))
        self.assertIs(target, msg.channel)                  # 失敗時は元chへ
        self.assertEqual(msg.channel.sent, ["本文"])


if __name__ == "__main__":
    unittest.main()
