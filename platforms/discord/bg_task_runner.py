#!/usr/bin/env python3
"""裏の作業の Discord 側。AgentClient に混ぜる mixin。

返答の [TASK: …] を受けてスレッドを開き、裏で claude を動かし、途中経過を数分ごとに、
結果と成果物ファイルを最後に置く。判定・文言・台帳は core/bg_tasks.py。

claude の起動は core/invoke_claude の行読み経路（Popen＋スレッド）を使うので、
Windows でもそのまま動く（select や killpg は使わない）。
"""

import asyncio
import shutil
import tempfile
import threading
import time

import discord

from core import bg_tasks
from core import db
from core import invoke_claude
from core import llm
from core import misses
from core import search
from core.archive_tools import launch as tool_launch
from core.archive_tools.context import ToolContext
from platforms.discord.agent_runtime import DB_PATH, GUILD_ID, _open_thread

FALLBACK_FILESIZE_LIMIT = 8 * 1024 * 1024
# 作業の終わりを見に行く間隔（秒）。途中経過の間隔とは別（そちらは設定で分単位）
POLL_SEC = 10


class BgTaskMixin:
    """self.* は bot.py の AgentClient の属性（agent・persona_files・_bg_tasks 等）。"""

    def _bg_cfg(self):
        """有効なら設定（上限つき）、無効なら None。既定はオフ。"""
        return bg_tasks.normalize((self.agent.get("skills") or {}).get("bg_tasks"))

    def _bg_available(self):
        """引き受けてよいか（有効・Claude Code が使える）。指示文を入れるかの判定にも使う。
        他のAIを選んでいると道具が使えないので、引き受けると約束だけになってしまう。"""
        return self._bg_cfg() is not None and invoke_claude.check_available() is None

    def _bg_tool_plan(self, message):
        """社内ログ等は読む道具だけ（dry_run＝書き込み・送信系は見せない）。"""
        skills = getattr(self, "_tool_skills", lambda: frozenset())()
        ctx = ToolContext(
            agent_id=self.agent["id"], actor_id=str(message.author.id),
            db_path=DB_PATH, guild_id=str(GUILD_ID),
            channel_id=message.channel.id, message_id=message.id,
            dry_run=True, skills=skills)
        return tool_launch.build(ctx)

    async def _start_bg_task(self, message, instruction):
        """引き受けた作業をスレッドで始める（使えない・同時数を超えたら正直に断る）。"""
        cfg = self._bg_cfg() or bg_tasks.normalize(True)
        reason = invoke_claude.check_available()
        if reason is not None:
            await message.channel.send(
                "-# ⚠️ 裏の作業は Claude Code を使うときだけ引き受けられます。"
                f"この作業は始められませんでした（{reason[:120]}）",
                allowed_mentions=discord.AllowedMentions.none())
            return
        limit = cfg["max_parallel"]
        if await asyncio.to_thread(bg_tasks.running_count, DB_PATH,
                                   self.agent["id"]) >= limit:
            await message.channel.send(
                f"-# ⚠️ いま裏で作業が{limit}件進んでいて、この作業は始められませんでした。"
                "終わったらもう一度頼んでください",
                allowed_mentions=discord.AllowedMentions.none())
            return
        thread = await _open_thread(message, f"🛠 {instruction[:60]}")
        task_id = await asyncio.to_thread(
            bg_tasks.create, DB_PATH, agent_id=self.agent["id"],
            channel_id=message.channel.id, thread_id=getattr(thread, "id", None),
            requester_id=message.author.id, instruction=instruction)
        await thread.send(f"-# 🛠 作業を始めました（task#{task_id}）。"
                          "時間がかかるときは、進み具合をここに書きます",
                          allowed_mentions=discord.AllowedMentions.none())
        task = asyncio.create_task(
            self._run_bg_task(task_id, message, thread, instruction, cfg))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _run_bg_task(self, task_id, message, thread, instruction, cfg):
        workdir = tempfile.mkdtemp(prefix=f"bg-{task_id}-")
        steps, lock = [], threading.Lock()

        def on_event(ev):                       # claude を読むスレッドから呼ばれる
            names = bg_tasks.steps_from_event(ev)
            if names:
                with lock:
                    steps.extend(names)

        try:
            plan = self._bg_tool_plan(message)
            system = (search.load_persona(self.persona_files)
                      + search._build_system(search.GENERAL_SYSTEM_TMPL,
                                             {"name": self.agent["name"], "role": ""}))
            model = llm.model_for(invoke_claude._config(), "claude")
            timeout = cfg["timeout_min"] * 60
            interval = cfg["report_interval_min"] * 60
            started = time.monotonic()
            job = asyncio.create_task(asyncio.to_thread(
                invoke_claude.invoke,
                bg_tasks.worker_prompt(self.agent["name"], instruction),
                model=model or invoke_claude.DEFAULT_MODEL, system=system,
                allowed_tools=bg_tasks.TOOLS,
                allow=bg_tasks.allow_rules(workdir, plan.allow),
                mcp_config=plan.mcp_config, cwd=workdir, timeout=timeout,
                on_event=on_event, max_budget_usd=bg_tasks.MAX_BUDGET_USD,
                purpose="bg_task"))
            last_at, reported = started, 0
            while not job.done():
                await asyncio.sleep(POLL_SEC)
                now = time.monotonic()
                with lock:
                    snapshot = list(steps)
                if bg_tasks.due_report(now=now - started, last_at=last_at - started,
                                       interval=interval,
                                       changed=len(snapshot) != reported):
                    await thread.send(
                        bg_tasks.progress_text(now - started,
                                               bg_tasks.label_steps(snapshot)),
                        allowed_mentions=discord.AllowedMentions.none())
                    last_at, reported = now, len(snapshot)
            result = job.result()
            await self._deliver_bg_result(task_id, message, thread, workdir,
                                          result.text or "", time.monotonic() - started)
        except Exception as e:      # noqa: BLE001 - 失敗は正直に知らせる
            await self._fail_bg_task(task_id, message, thread, workdir, instruction, e)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _filesize_limit(self, message):
        guild = getattr(message, "guild", None)
        return guild.filesize_limit if guild is not None else FALLBACK_FILESIZE_LIMIT

    async def _send_files(self, thread, workdir, message):
        files, skipped = bg_tasks.output_files(workdir, self._filesize_limit(message))
        if files:
            await thread.send(files=[discord.File(p) for p in files],
                              allowed_mentions=discord.AllowedMentions.none())
        if skipped:
            await thread.send("-# ⚠️ 大きすぎて添付できなかったファイル: " + "、".join(skipped),
                              allowed_mentions=discord.AllowedMentions.none())
        return files

    async def _deliver_bg_result(self, task_id, message, thread, workdir, text, took):
        """完了の知らせはシステムが書く（本文はモデルの報告をそのまま添える）。"""
        minutes = max(1, int(took // 60))
        head = f"<@{message.author.id}> ✅ 作業が終わりました（task#{task_id}・{minutes}分）\n"
        body = (text or "（報告文がありませんでした）").strip()
        await thread.send((head + body)[:1990],
                          allowed_mentions=discord.AllowedMentions(users=True))
        for i in range(1990 - len(head), len(body), 1990):
            await thread.send(body[i:i + 1990],
                              allowed_mentions=discord.AllowedMentions.none())
        await self._send_files(thread, workdir, message)
        await asyncio.to_thread(bg_tasks.finish, DB_PATH, task_id, "done", body)

    async def _fail_bg_task(self, task_id, message, thread, workdir, instruction, err):
        reason = f"{type(err).__name__}: {err}"[:200]
        if "timeout" in reason.lower() or "タイムアウト" in reason:
            reason = "時間切れ（上限に達した）"
        try:
            await thread.send(f"<@{message.author.id}> ⚠️ 作業を最後までできませんでした"
                              f"（task#{task_id}・{reason}）。"
                              "作りかけのファイルがあれば置いておきます",
                              allowed_mentions=discord.AllowedMentions(users=True))
            await self._send_files(thread, workdir, message)
        except discord.DiscordException as e:
            print(f"[{self.agent['id']}] bg task failure notice failed: {e}")
        await asyncio.to_thread(bg_tasks.finish, DB_PATH, task_id, "failed", reason)
        await asyncio.to_thread(
            misses.record_gap, DB_PATH, agent_id=self.agent["id"],
            source="bg_task_failed", context=f"裏の作業: {instruction[:80]}",
            detail=reason, topic="bg_task")

    async def _recover_bg_tasks(self):
        """再起動で途中になった作業を正直に知らせる（続きからはやらない）。
        起動処理（on_ready）の途中で呼ばれるので、何があっても例外を外へ出さない
        （出すと後続の観察ループ等が起動しない）。表がまだ無い初回もあるので先に作る
        （会話を記録する担当以外は on_ready で init_db を呼ばない）。"""
        try:
            await asyncio.to_thread(db.init_db, DB_PATH)
            stale = await asyncio.to_thread(bg_tasks.interrupt_running, DB_PATH,
                                            self.agent["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[{self.agent['id']}] bg task recover failed: {e}")
            return
        for t in stale:
            try:
                ch = (self.get_channel(t["thread_id"])
                      or await self.fetch_channel(t["thread_id"]))
                await ch.send(f"<@{t['requester_id']}> ⚠️ 再起動で作業が途中で止まりました"
                              f"（task#{t['id']}）。もう一度頼んでもらえれば最初からやります",
                              allowed_mentions=discord.AllowedMentions(users=True))
            except Exception as e:  # noqa: BLE001 - 1件の失敗で起動処理を止めない
                print(f"[{self.agent['id']}] bg task recover notice failed: {e}")
