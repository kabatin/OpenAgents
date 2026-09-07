#!/usr/bin/env python3
"""観察ループの遅延隔離（_run_cycle）のユニットテスト。
1サイクルの超過・例外が後続サイクルを止めないことを検証する。"""

import asyncio
import unittest
from unittest import mock

from platforms.discord import agent_loops


class _Client(agent_loops.AgentLoopsMixin):
    def __init__(self):
        self.agent = {"id": "agent1"}


class RunCycleTest(unittest.TestCase):
    def test_timeout_is_logged_and_does_not_stop_next_cycle(self):
        client = _Client()
        order = []

        async def slow():
            await asyncio.sleep(10)

        async def fast():
            order.append("fast")

        logged = []

        def fake_log(db_path, agent_id, **kw):
            logged.append(kw)

        async def run():
            with mock.patch.object(agent_loops, "CYCLE_TIMEOUT_SEC", 0.01), \
                    mock.patch.object(agent_loops.proactive, "log_entry",
                                      fake_log):
                await client._run_cycle("slow", slow)
                await client._run_cycle("fast", fast)
        asyncio.run(run())
        self.assertEqual(order, ["fast"])
        self.assertEqual(logged[0]["action"], "cycle_timeout")
        self.assertEqual(logged[0]["detail"], "slow")

    def test_exception_is_swallowed(self):
        client = _Client()

        async def boom():
            raise RuntimeError("x")

        asyncio.run(client._run_cycle("boom", boom))   # 例外が漏れない


if __name__ == "__main__":
    unittest.main()
