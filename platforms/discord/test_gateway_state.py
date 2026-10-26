#!/usr/bin/env python3
"""Discord の接続・切断イベントが、つながり具合の記録（ConnectionTracker）に届くこと。

ネット断のあと、プロセスは生きたまま接続だけ戻らないことがある。スーパーバイザは
この記録を見て再起動するので、イベントの配線が切れると固着を拾えなくなる。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from platforms.discord import bot
from platforms.discord.dev import bot as dev_bot


class FakeTracker:
    def __init__(self):
        self.calls = []

    def connected(self, key):
        self.calls.append(("up", key))

    def disconnected(self, key):
        self.calls.append(("down", key))


class GatewayEventsTest(unittest.TestCase):
    def test_conversation_bot_reports_each_agent(self):
        fake = FakeTracker()
        me = SimpleNamespace(agent={"id": "agent1"})
        with mock.patch.object(bot, "GATEWAY", fake):
            asyncio.run(bot.AgentClient.on_connect(me))
            asyncio.run(bot.AgentClient.on_disconnect(me))
            asyncio.run(bot.AgentClient.on_resumed(me))
        self.assertEqual(fake.calls, [("up", "agent1"), ("down", "agent1"),
                                      ("up", "agent1")])

    def test_dev_bot_reports(self):
        fake = FakeTracker()
        with mock.patch.object(dev_bot, "GATEWAY", fake):
            asyncio.run(dev_bot.DevBot.on_disconnect(SimpleNamespace()))
            asyncio.run(dev_bot.DevBot.on_connect(SimpleNamespace()))
        self.assertEqual(fake.calls, [("down", "devbot"), ("up", "devbot")])


if __name__ == "__main__":
    unittest.main()
