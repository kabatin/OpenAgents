#!/usr/bin/env python3
"""bot の束ね処理: 連投をまとめて1回だけ応答する（偽の投稿で確かめる）。"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from platforms.discord import bot


class WaiterTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_posts_become_one_answer(self):
        calls = []

        async def handled(message, trigger, teach, question=None, extra_messages=()):
            calls.append((message.id, question, [m.id for m in extra_messages]))
        fake = SimpleNamespace(agent={"id": "agent1"}, turn_wait_max=180,
                               _turn_buffers={}, _handle_triggered=handled)
        m1 = SimpleNamespace(id=1, clean_content="明日の件なんだけど、")
        m2 = SimpleNamespace(id=2, clean_content="会場の候補を比較して表にして")
        now = time.monotonic()
        fake._turn_buffers[(9, 5)] = {"msgs": [m1], "trigger": "home", "teach": None,
                                      "first_at": now, "last_msg_at": now,
                                      "typing_at": now}          # 入力中
        task = asyncio.create_task(bot.AgentClient._turn_waiter(fake, (9, 5)))
        await asyncio.sleep(1.2)
        self.assertEqual(calls, [])                               # 言いかけ＋入力中は待つ
        buf = fake._turn_buffers[(9, 5)]
        buf["msgs"].append(m2)
        buf["last_msg_at"] = time.monotonic() - 2.5               # 2.5秒前に言い終わった
        buf["typing_at"] = time.monotonic() - 13                  # 入力は止まった
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(calls, [(2, "明日の件なんだけど、\n会場の候補を比較して表にして", [1])])
        self.assertEqual(fake._turn_buffers, {})

    async def test_failure_clears_buffer_and_does_not_raise(self):
        async def boom(*a, **k):
            raise RuntimeError("x")
        fake = SimpleNamespace(agent={"id": "agent1"}, turn_wait_max=1,
                               _turn_buffers={}, _handle_triggered=boom)
        m = SimpleNamespace(id=1, clean_content="お願いします")
        past = time.monotonic() - 10
        fake._turn_buffers[(1, 1)] = {"msgs": [m], "trigger": "home", "teach": None,
                                      "first_at": past, "last_msg_at": past,
                                      "typing_at": None}
        await asyncio.wait_for(bot.AgentClient._turn_waiter(fake, (1, 1)), timeout=5)
        self.assertEqual(fake._turn_buffers, {})


if __name__ == "__main__":
    unittest.main()
