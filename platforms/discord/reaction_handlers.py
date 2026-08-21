#!/usr/bin/env python3
"""リアクション処理（feedback収集・教訓帳フック・納期追跡の✅❌）。

bot.py から分離した AgentClient の mixin（archiver=アーカイブ担当だけが実際に使う）。
採用/解雇提案の👍は WebhookPersonaMixin 側にある。新しいリアクション起点の
機能はここに足す。"""

import asyncio

from core import ab_test
from core import action_items
from core import auto_discover
from core import db
from core import event_planner
from core import golden
from core import ripple
from core import study_group
from core import proactive
from core import rule_distill
from core import reminders
from platforms.discord.agent_runtime import (
    ADMIN_IDS,
    AGENT_USER_IDS,
    ALLOWED_MENTIONS,
    DB_PATH,
    GUILD_ID,
    NEGATIVE_REACTIONS,
    POSITIVE_REACTIONS,
    registered_bot_ids,
)

class ReactionHandlersMixin:
    """AgentClient に混ぜる mixin（self.* は bot.py の属性・設定を参照する）。"""

    async def on_raw_reaction_add(self, payload):
        # 未キャッシュのメッセージにも効くよう raw を使う（archiverのみ）
        if self.is_archiver:
            await self._record_reaction(payload, added=True)
            # 採用・解雇提案への管理者👍を承認として処理（best-effort）
            try:
                await self._maybe_approve_hire(payload)
                await self._maybe_approve_fire(payload)
            except Exception as e:
                print(f"[hire/fire] approve failed: {e}")
            # 納期追跡: 声かけへの✅完了・追跡宣言への❌取り消し（Phase B）
            try:
                await self._maybe_action_item_reaction(payload)
            except Exception as e:
                print(f"[action_items] reaction failed: {e}")
            # イベント逆算提案への✅❌（RM#35）
            try:
                await self._maybe_event_reaction(payload)
            except Exception as e:
                print(f"[event_planner] reaction failed: {e}")
            # ルール棚卸し提案への✅❌（RM#2・管理者のみ）
            try:
                await self._maybe_rule_review_reaction(payload)
            except Exception as e:
                print(f"[rule_distill] reaction failed: {e}")
            # 自動化アイデアへの👍起票❌見送り（RM#24・管理者のみ）
            try:
                await self._maybe_auto_proposal_reaction(payload)
            except Exception as e:
                print(f"[auto_discover] reaction failed: {e}")
            # 勉強会（ルールのglobal昇格）への✅❌（RM#61・管理者のみ）
            try:
                await self._maybe_share_reaction(payload)
            except Exception as e:
                print(f"[study_group] reaction failed: {e}")
            # 波及チェッカーへの✅❌（#101・管理者のみ）
            try:
                await self._maybe_ripple_reaction(payload)
            except Exception as e:
                print(f"[ripple] reaction failed: {e}")

    async def on_raw_reaction_remove(self, payload):
        if self.is_archiver:
            await self._record_reaction(payload, added=False)

    async def _maybe_action_item_reaction(self, payload):
        """納期追跡への人間のリアクション処理（Phase B）。
        ✅=声かけ対象タスクの完了 / ❌=追跡宣言の一括取り消し（管理者のみ）。"""
        if payload.guild_id != GUILD_ID:
            return
        if payload.user_id in registered_bot_ids():
            return
        emoji = str(payload.emoji)
        if emoji == "✅":
            item = await asyncio.to_thread(
                action_items.complete_by_nudge_message, DB_PATH,
                payload.message_id)
            if item:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send(
                    f"-# 📗 タスク完了として記録したっス: {item['task'][:60]}",
                    allowed_mentions=ALLOWED_MENTIONS)
        elif emoji == "❌" and str(payload.user_id) in ADMIN_IDS:
            n = await asyncio.to_thread(
                action_items.drop_by_confirm_message, DB_PATH,
                payload.message_id)
            if n:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send(
                    f"-# 🗑 この議事録のタスク追跡{n}件を取り消したっス",
                    allowed_mentions=ALLOWED_MENTIONS)

    async def _maybe_event_reaction(self, payload):
        """イベント逆算提案への✅承認（納期追跡へ登録）/❌見送り（RM#35・管理者のみ）。"""
        if payload.guild_id != GUILD_ID:
            return
        if str(payload.user_id) not in ADMIN_IDS:
            return
        emoji = str(payload.emoji)
        if emoji == "✅":
            n = await asyncio.to_thread(
                event_planner.approve, DB_PATH, self.agent["id"],
                payload.message_id)
            if n:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send(
                    f"-# 🗓️ 逆算スケジュール{n}件を納期追跡に載せたっス"
                    "（期日2日前と当日に声かけするっスね）",
                    allowed_mentions=ALLOWED_MENTIONS)
        elif emoji == "❌":
            if await asyncio.to_thread(
                    event_planner.dismiss, DB_PATH, payload.message_id):
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send("-# 🗓️ 逆算案は見送りにしたっス",
                                   allowed_mentions=ALLOWED_MENTIONS)

    async def _maybe_rule_review_reaction(self, payload):
        """ルール棚卸し提案への✅一括無効化／❌見送り（RM#2・管理者のみ）。"""
        if (payload.guild_id != GUILD_ID
                or str(payload.user_id) not in ADMIN_IDS):
            return
        emoji = str(payload.emoji)
        if emoji == "✅":
            n = await asyncio.to_thread(
                rule_distill.approve, DB_PATH, payload.message_id)
            if n is not None:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send(f"-# 🧹 ルール{n}件を無効化したっス",
                                   allowed_mentions=ALLOWED_MENTIONS)
        elif emoji == "❌":
            if await asyncio.to_thread(
                    rule_distill.dismiss, DB_PATH, payload.message_id):
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send("-# 🧹 棚卸しは今回見送りっス",
                                   allowed_mentions=ALLOWED_MENTIONS)

    async def _maybe_auto_proposal_reaction(self, payload):
        """自動化アイデア提案への👍（起票→開発BOTへ）/❌見送り（RM#24・管理者のみ）。"""
        if (payload.guild_id != GUILD_ID
                or str(payload.user_id) not in ADMIN_IDS):
            return
        emoji = str(payload.emoji)
        if emoji == "👍":
            ids = await asyncio.to_thread(
                auto_discover.approve, DB_PATH, self.agent["id"],
                payload.message_id, payload.user_id)
            if ids:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                nums = "、".join(f"#{i}" for i in ids)
                await channel.send(
                    f"-# 🧩 起票{nums}を作ったっス（開発BOTが拾って提案するっスよ）",
                    allowed_mentions=ALLOWED_MENTIONS)
        elif emoji == "❌":
            if await asyncio.to_thread(
                    auto_discover.dismiss, DB_PATH, payload.message_id):
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send("-# 🤖 自動化案は見送りっス",
                                   allowed_mentions=ALLOWED_MENTIONS)

    async def _maybe_ripple_reaction(self, payload):
        """波及提案への✅（矛盾する旧決定をsuperseded化）/❌（#101・管理者のみ）。"""
        if (payload.guild_id != GUILD_ID
                or str(payload.user_id) not in ADMIN_IDS):
            return
        emoji = str(payload.emoji)
        if emoji == "✅":
            n = await asyncio.to_thread(
                ripple.approve, DB_PATH, payload.message_id)
            if n is not None:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                note = (f"旧決定{n}件を上書き済みにしたっス" if n
                        else "確認済みにしたっス")
                await channel.send(f"-# 🌊 {note}",
                                   allowed_mentions=ALLOWED_MENTIONS)
        elif emoji == "❌":
            if await asyncio.to_thread(
                    ripple.dismiss, DB_PATH, payload.message_id):
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send("-# 🌊 誤検知として見送りっス"
                                   "（教訓として覚えるっス）",
                                   allowed_mentions=ALLOWED_MENTIONS)

    async def _maybe_share_reaction(self, payload):
        """勉強会提案への✅（ルールをglobalへ昇格）/❌（RM#61・管理者のみ）。"""
        if (payload.guild_id != GUILD_ID
                or str(payload.user_id) not in ADMIN_IDS):
            return
        emoji = str(payload.emoji)
        if emoji == "✅":
            n = await asyncio.to_thread(
                study_group.approve, DB_PATH, payload.message_id)
            if n:
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send(
                    f"-# 📚 ルール{n}件を全体ルールに昇格したっス"
                    "（みんなに共有されるっス）",
                    allowed_mentions=ALLOWED_MENTIONS)
        elif emoji == "❌":
            if await asyncio.to_thread(
                    study_group.dismiss, DB_PATH, payload.message_id):
                channel = (self.get_channel(payload.channel_id)
                           or await self.fetch_channel(payload.channel_id))
                await channel.send("-# 📚 共有は見送りっス",
                                   allowed_mentions=ALLOWED_MENTIONS)

    async def _record_reaction(self, payload, added):
        """いずれかのエージェントの投稿への👍👎を feedback に記録（物差しの
        原料）。リアクション対象の著者はアーカイブDBから引く
        （rawイベントには著者が無いため）。"""
        if payload.guild_id != GUILD_ID:
            return
        emoji = str(payload.emoji)
        if emoji in POSITIVE_REACTIONS:
            value = "up"
        elif emoji in NEGATIVE_REACTIONS:
            value = "down"
        else:
            return  # 集計対象外の絵文字は無視
        if payload.user_id in registered_bot_ids():
            return  # Bot自身のリアクションは数えない
        try:
            with db.connect(DB_PATH) as conn:
                author_id = db.message_author(conn, payload.message_id)
                agent_id = next((aid for aid, uid in AGENT_USER_IDS.items()
                                 if uid == author_id), None)
                if agent_id is None:
                    return  # エージェントの投稿以外は対象外
                if added:
                    db.add_feedback(
                        conn, message_id=payload.message_id, agent_id=agent_id,
                        kind="reaction", value=value,
                        user_id=str(payload.user_id),
                        created_at=reminders.fmt(reminders.now_jst()))
                else:
                    db.remove_feedback(
                        conn, message_id=payload.message_id,
                        user_id=str(payload.user_id), value=value)
            print(f"[{self.agent['id']}] feedback {'+'if added else '-'}"
                  f"{value} on {agent_id}'s msg {payload.message_id}")
            # A/B実験（RM#12）: その発言を生んだ変種へ👍👎を帰属させる
            if added:
                vid = await asyncio.to_thread(
                    ab_test.record_feedback_for_message, DB_PATH,
                    payload.message_id, value)
                if vid:
                    print(f"[{self.agent['id']}] A/B feedback {value} "
                          f"→ variant {vid}")
            # 👍つき回答はゴールデンセットへ自動蓄積（RM#16・静かなデータ）
            if value == "up" and added:
                saved = await asyncio.to_thread(
                    golden.capture, DB_PATH, agent_id, payload.message_id)
                if saved:
                    print(f"[{self.agent['id']}] golden Q&A captured")
            # 自発発言への👍は勝ちパターンとして自動記録（教訓帳RM#7の対称）。
            # 👍全解除で引っ込む
            if value == "up":
                if added:
                    kind = await asyncio.to_thread(
                        proactive.record_win_from_feedback, DB_PATH,
                        payload.message_id)
                    if kind:
                        print(f"[{self.agent['id']}] proactive win "
                              f"recorded ({kind})")
                else:
                    await asyncio.to_thread(
                        proactive.lift_win_if_no_ups, DB_PATH,
                        payload.message_id)
            # 自発発言への👎は教訓として自動記録（RM#7）。👎全解除で教訓も解除
            if value == "down":
                if added:
                    kind = await asyncio.to_thread(
                        proactive.record_lesson_from_feedback, DB_PATH,
                        payload.message_id)
                    if kind:
                        print(f"[{self.agent['id']}] proactive lesson "
                              f"recorded ({kind})")
                else:
                    await asyncio.to_thread(
                        proactive.lift_lesson_if_no_downs, DB_PATH,
                        payload.message_id)
        except Exception as e:
            print(f"[{self.agent['id']}] reaction record failed: {e}")
