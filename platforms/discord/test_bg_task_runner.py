#!/usr/bin/env python3
"""裏の作業の Discord 側（platforms/discord/bg_task_runner.py）。

Discord・claude は偽物で代用し、引き受け・断り・結果の受け渡し・失敗・再起動後の
知らせを確かめる。
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from core import bg_tasks
from core import db
from core import misses
from platforms.discord import bg_task_runner


class FakeThread:
    def __init__(self, tid=500):
        self.id = tid
        self.sent = []

    async def send(self, content=None, *, files=None, allowed_mentions=None):
        self.sent.append({"content": content,
                          "files": [os.path.basename(f.filename) for f in files or []]})
        return NS(id=len(self.sent))


class FakeClient(bg_task_runner.BgTaskMixin):
    def __init__(self, db_path, cfg):
        self.agent = {"id": "agent1", "name": "エージェント1",
                      "skills": {"bg_tasks": cfg}}
        self.persona_files = []
        self._bg_tasks = set()
        self.channels = {}

    def _tool_skills(self):
        return frozenset()

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise RuntimeError("not found")


def _message(channel):
    return NS(id=42, author=NS(id=9, bot=False), channel=channel,
              guild=NS(filesize_limit=8 * 1024 * 1024), clean_content="表を作って")


class RunnerTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        for name, value in (("DB_PATH", self.db_path), ("POLL_SEC", 0.01)):
            p = mock.patch.object(bg_task_runner, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.thread = FakeThread()
        self.channel = FakeThread(tid=100)

        async def open_thread(message, name):
            return self.thread
        p = mock.patch.object(bg_task_runner, "_open_thread", open_thread)
        p.start()
        self.addCleanup(p.stop)
        # Claude Code が使える前提（使えないケースは個別に差し替える）
        p = mock.patch.object(bg_task_runner.invoke_claude, "check_available",
                              lambda cfg=None: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        os.unlink(self.db_path)

    def client(self, cfg=None):
        return FakeClient(self.db_path, cfg if cfg is not None else {"enabled": True})

    async def _drain(self, client):
        while client._bg_tasks:
            await asyncio.gather(*list(client._bg_tasks))


class StartTest(RunnerTestBase):
    async def test_over_the_limit_is_declined_honestly(self):
        client = self.client({"enabled": True, "max_parallel": 1})
        bg_tasks.create(self.db_path, agent_id="agent1", channel_id=1, thread_id=2,
                        requester_id="9", instruction="先の作業")
        await client._start_bg_task(_message(self.channel), "表を作る")
        self.assertIn("始められませんでした", self.channel.sent[0]["content"])
        self.assertEqual(self.thread.sent, [])        # スレッドは開かない

    async def test_other_provider_is_declined_honestly(self):
        client = self.client()
        with mock.patch.object(bg_task_runner.invoke_claude, "check_available",
                               lambda cfg=None: "Codex CLI では使えません"):
            await client._start_bg_task(_message(self.channel), "表を作る")
        self.assertIn("Claude Code", self.channel.sent[0]["content"])
        self.assertEqual(bg_tasks.running_count(self.db_path, "agent1"), 0)


class RunTest(RunnerTestBase):
    async def test_result_and_files_are_handed_over(self):
        client = self.client()
        seen = {}

        def fake_invoke(prompt, **kw):
            seen.update(kw)
            with open(os.path.join(kw["cwd"], "比較表.md"), "w", encoding="utf-8") as f:
                f.write("| 会場 | 価格 |")
            kw["on_event"]({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "WebSearch"}]}})
            return NS(text="比較表.md を作りました。要点は…")
        with mock.patch.object(bg_task_runner.invoke_claude, "invoke", fake_invoke):
            await client._start_bg_task(_message(self.channel), "会場の比較表を作る")
            await self._drain(client)
        contents = [m["content"] or "" for m in self.thread.sent]
        self.assertIn("作業を始めました", contents[0])
        done = next(c for c in contents if "作業が終わりました" in c)
        self.assertIn("<@9>", done)                          # 依頼者に知らせる
        self.assertIn("比較表.md を作りました", done)
        self.assertIn(["比較表.md"], [m["files"] for m in self.thread.sent])
        # 書き込みは作業フォルダの中だけ・Bash は渡さない
        self.assertNotIn("Bash", seen["allowed_tools"])
        self.assertTrue(any(a.startswith("Write(//") for a in seen["allow"]))
        task = bg_tasks.get(self.db_path, 1)
        self.assertEqual(task["status"], "done")
        self.assertFalse(os.path.exists(seen["cwd"]))         # 作業フォルダは片付ける

    async def test_failure_is_told_and_recorded(self):
        client = self.client()

        def boom(prompt, **kw):
            raise RuntimeError("claude CLI 失敗 (exit=1): rate limited")
        with mock.patch.object(bg_task_runner.invoke_claude, "invoke", boom):
            await client._start_bg_task(_message(self.channel), "調べもの")
            await self._drain(client)
        last = self.thread.sent[-1]["content"]
        self.assertIn("最後までできませんでした", last)
        self.assertEqual(bg_tasks.get(self.db_path, 1)["status"], "failed")
        with db.connect(self.db_path) as conn:
            rows = conn.execute("SELECT source, topic FROM misses").fetchall()
        self.assertEqual(rows, [("bg_task_failed", "bg_task")])
        self.assertTrue(misses)   # 台帳モジュールを使っていること（import の確認）


class RecoverTest(RunnerTestBase):
    async def test_recover_never_raises_into_on_ready(self):
        # 表がまだ無いDBでも例外を外へ出さない（出すと後続の起動処理が止まる）
        client = self.client()
        fresh = os.path.join(tempfile.mkdtemp(), "fresh.db")
        with mock.patch.object(bg_task_runner, "DB_PATH", fresh):
            await client._recover_bg_tasks()
            self.assertEqual(bg_tasks.running_count(fresh, "agent1"), 0)

    async def test_interrupted_tasks_are_reported(self):
        client = self.client()
        client.channels[500] = self.thread
        bg_tasks.create(self.db_path, agent_id="agent1", channel_id=1, thread_id=500,
                        requester_id="9", instruction="表を作る")
        await client._recover_bg_tasks()
        self.assertIn("再起動で作業が途中で止まりました", self.thread.sent[0]["content"])
        self.assertEqual(bg_tasks.get(self.db_path, 1)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
