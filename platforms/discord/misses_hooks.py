#!/usr/bin/env python3
"""失敗と間違いの台帳への入口（返信で受け取るもの）。AgentClient に混ぜる mixin。

- ❌した提案（または理由を聞いた投稿）への返信 → 理由として台帳に貯める（📝だけ返す）
- 決定の波及チェックの投稿への返信 → 「直し方を教わった」依頼としてエージェントが
  対応し、前後のリマインダー・タスクの変化を、人の指示と一緒に見本として残す。
  同じ場面の「自動では直せなかった」は答え済みになる（core/misses.py）
"""

import asyncio

import discord

from core import misses
from core import ripple
from platforms.discord.agent_runtime import DB_PATH


class MissesHooksMixin:
    """AgentClient に混ぜる mixin（self.* は bot.py の属性を参照する）。"""

    async def _maybe_miss_reason(self, message):
        """❌した提案（または理由を聞いた投稿）への返信を理由として受け取る。
        エージェントへのメンションつきなら通常の依頼として扱う（False）。"""
        if (message.author.bot or message.reference is None
                or not message.reference.message_id
                or (self.user is not None
                    and self.user.id in message.raw_mentions)):
            return False
        saved = await asyncio.to_thread(
            misses.save_reason, DB_PATH, message.reference.message_id,
            message.content or "", message.author.id)
        if not saved:
            return False
        try:
            await message.add_reaction("📝")    # 受け取ったことだけ静かに返す
        except discord.DiscordException:
            pass
        return True

    async def _teaching_context(self, message):
        """決定の波及チェックの投稿への返信なら、その決定の「場面」を返す。
        人が直し方を教えてくれた＝エージェントが直した結果を正解の見本として残す。"""
        if (not self.is_archiver or message.author.bot
                or message.reference is None
                or not message.reference.message_id):
            return None
        decision = await asyncio.to_thread(
            ripple.decision_for, DB_PATH, message.reference.message_id)
        return ripple.gap_context(decision) if decision else None

    async def _record_teaching(self, message, context, before):
        """教わって直した前後の変化を、失敗と間違いの台帳へ（変化が無ければ残さない）。"""
        try:
            after = await asyncio.to_thread(misses.snapshot, DB_PATH)
            changes = misses.diff_snapshots(before, after)
            if not changes:
                return
            await asyncio.to_thread(
                misses.record_teaching, DB_PATH, agent_id=self.agent["id"],
                context=context, instruction=message.clean_content or "",
                changes=changes)
            print(f"[{self.agent['id']}] 直し方の見本を記録（{len(changes)}件）")
        except Exception as e:      # 記録の失敗で返答は止めない
            print(f"[{self.agent['id']}] teaching record failed: {e}")
