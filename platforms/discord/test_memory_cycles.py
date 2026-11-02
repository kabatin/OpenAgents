#!/usr/bin/env python3
"""観察ループの記憶まわりの配線テスト。

- 一次判定のついでに拾った事実: シャドーは自発ログだけ、本番は台帳へ
- 要約の追いつき: 未反映の多いチャンネルを1つだけ更新する
- 一次判定が失敗したら checkpoint を戻す
"""

import asyncio
import unittest
from unittest import mock

from platforms.discord import agent_loops


class _Client(agent_loops.AgentLoopsMixin):
    def __init__(self, proactive_cfg=None):
        self.agent = {"id": "agent1", "name": "アシスタント"}
        self.proactive_cfg = proactive_cfg or {}
        self.home_channel_id = 100
        self.is_archiver = False


DIGEST = {"messages": [{"id": 7, "channel_id": 5, "channel": "g",
                        "author": "ユーザー", "content": "送料は800円に変更"}],
          "quota_left": 3, "checkpoint_from": 2}
SCREENED = {"candidates": [], "decisions": [], "handoffs": [], "others": [],
            "facts": [{"message_id": 7, "fact": "国内送料は800円",
                       "topic": "送料"},
                      {"message_id": 99, "fact": "範囲外", "topic": "x"}]}


class ScreenFactsWiringTest(unittest.TestCase):
    def _run(self, cfg):
        logged, saved = [], []

        def fake_log(db_path, agent_id, **kw):
            logged.append(kw)

        def fake_save(db_path, agent_id, items):
            saved.append(items)
            return [(1, "送料", "国内送料は800円")]

        with mock.patch.object(agent_loops.proactive, "log_entry", fake_log), \
                mock.patch.object(agent_loops.facts, "save_extracted",
                                  fake_save):
            asyncio.run(_Client()._save_screen_facts(SCREENED, DIGEST, cfg))
        return logged, saved

    def test_default_is_shadow_and_does_not_write(self):
        logged, saved = self._run({})
        self.assertEqual(saved, [])
        self.assertEqual(logged[0]["kind"], "facts_extract")
        self.assertEqual(logged[0]["action"], "shadow")
        self.assertIn("[送料] 国内送料は800円", logged[0]["detail"])
        self.assertNotIn("範囲外", logged[0]["detail"])

    def test_live_writes_with_provenance(self):
        logged, saved = self._run({"facts_extract": {"enabled": True,
                                                     "shadow": False}})
        self.assertEqual(saved[0], [{"message_id": 7, "fact": "国内送料は800円",
                                     "topic": "送料", "channel_id": 5,
                                     "stated_by": "ユーザー"}])
        self.assertEqual(logged[0]["action"], "saved")

    def test_disabled_does_nothing(self):
        logged, saved = self._run({"facts_extract": {"enabled": False}})
        self.assertEqual((logged, saved), ([], []))


class SummariesCycleTest(unittest.TestCase):
    def test_updates_the_stalest_channel_only(self):
        calls, picked = [], []

        def fake_stale(conn, min_new, limit, exclude_channel_ids):
            picked.append((min_new, limit, exclude_channel_ids))
            return [{"channel_id": 3, "new_messages": 40}]

        def fake_update(db_path, channel_id):
            calls.append(channel_id)
            return "要約"

        client = _Client({"exclude_channel_ids": [9]})
        with mock.patch.object(agent_loops.db, "stale_summary_channels",
                               fake_stale), \
                mock.patch.object(agent_loops.summaries, "maybe_update",
                                  fake_update), \
                mock.patch.object(agent_loops.db, "connect",
                                  mock.MagicMock()):
            asyncio.run(client._summaries_cycle({"min_new_messages": 2}))
        self.assertEqual(calls, [3])
        # 要約側の下限より小さい設定は下限に丸める（同じchを選び続けない）
        self.assertEqual(picked, [(agent_loops.summaries.MIN_NEW_MESSAGES, 1,
                                   (9,))])

    def test_cycle_is_registered_by_default(self):
        names = [n for n, _ in _Client({})._cycle_plan()]
        self.assertIn("summaries", names)
        off = _Client({"summaries": {"enabled": False}})._cycle_plan()
        self.assertNotIn("summaries", [n for n, _ in off])


class ScreenFailureRewindTest(unittest.TestCase):
    def test_checkpoint_is_rewound_when_screen_fails(self):
        rewound = []

        def boom(*a, **kw):
            raise RuntimeError("auth")

        def fake_rewind(db_path, agent_id, to_id):
            rewound.append(to_id)
            return True

        client = _Client({})
        with mock.patch.object(agent_loops.proactive, "collect_cycle",
                               lambda *a, **kw: DIGEST), \
                mock.patch.object(agent_loops.proactive, "screen", boom), \
                mock.patch.object(agent_loops.proactive, "rewind_checkpoint",
                                  fake_rewind):
            with self.assertRaises(RuntimeError):
                asyncio.run(client._proactive_cycle())
        self.assertEqual(rewound, [2])


if __name__ == "__main__":
    unittest.main()
