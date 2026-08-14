#!/usr/bin/env python3
"""スキル・プリフック（YouTube要約・PDF自動要約）。

bot.py から分離した AgentClient の mixin。決定的トリガー（正規表現・添付種別）で
発動する「呼ばれなくても働く」スキル群。

ここに足すのは**組み込みのスキル**だけ。外部サービスを使うものは
integrations/ 側に PREHOOKS として書く（integrations.py 参照）。"""

import asyncio
import io

import discord

from core import attachments
from core import pdf_summary
from core import search
from core import youtube_summary
from platforms.discord.agent_runtime import (
    ADMIN_IDS,
    ALLOWED_MENTIONS,
    ANSWER_SEM,
    _best_effort_typing,
    _send_chunked,
    deliver_reply,
    registered_bot_ids,
)

# guild情報が取れない場合の添付サイズ上限（Discord無課金サーバー相当）
FALLBACK_FILESIZE_LIMIT = 8 * 1024 * 1024

class SkillHooksMixin:
    """AgentClient に混ぜる mixin（self.* は bot.py の属性・設定を参照する）。"""

    async def _run_integration_prehooks(self, message):
        """外部連携のプリフックを順に試す。処理した連携があれば True。

        フックは同期関数として書いてよい（ネットワークI/Oを含み得るので
        別スレッドで実行する）。文字列を返した場合はその場に投稿し、
        「このターンは処理済み」として通常の応答生成を止める。
        """
        ctx = self._integration_ctx(message)
        for integration in self.integrations:
            for name, fn in integration.prehooks:
                try:
                    got = await asyncio.to_thread(fn, ctx)
                except Exception as e:
                    print(f"[{self.agent['id']}] integration prehook "
                          f"{integration.name}:{name} failed: {e}")
                    continue
                if not got:
                    continue
                if isinstance(got, str):
                    await deliver_reply(message, got)
                return True
        return False

    async def _maybe_youtube_summary(self, message):
        """メッセージ中のYouTube URLを要約して返信。処理したらTrue。
        URLが無ければFalseで通常回答フローに流す。"""
        found = youtube_summary.extract_video_ids(message.content or "")
        if not found:
            return False
        video_id = found[0][0]
        user_text = youtube_summary.strip_urls(message.clean_content)
        try:
            async with ANSWER_SEM:
                async with _best_effort_typing(message.channel):
                    text = await asyncio.to_thread(
                        youtube_summary.summarize,
                        search.load_persona(self.persona_files),
                        video_id, user_text)
            if len(found) > 1:
                text += f"\n{youtube_summary.MULTI_URL_NOTE}"
            await _send_chunked(message.channel, text)
        except youtube_summary.TranscriptError as e:
            await message.channel.send(str(e))
        except Exception as e:
            print(f"[{self.agent['id']}] youtube summary failed: {e}")
            await message.channel.send(
                f"⚠️ 動画の要約に失敗したっス: {str(e)[:200]}")
        return True

    async def _maybe_pdf_summary(self, message):
        """メンション無しで投稿されたPDF添付を自動要約して投稿。処理したらTrue。
        通常フロー（メンション/ホームchのテキスト付き投稿）が応答するケースでは
        呼ばれない前提。Bot・webhookの投稿には反応せず、他エージェントが
        名指しされていたらその子の通常フローに譲る。"""
        if not self.pdf_summary or message.author.bot:
            return False
        if any(u.id in registered_bot_ids() for u in message.mentions):
            return False
        picked, overflow = pdf_summary.pick_pdfs(message.attachments)
        if not picked:
            return False
        tmpdir = None
        try:
            tmpdir, saved, failed = await attachments.download(picked)
            if not saved:
                print(f"[{self.agent['id']}] pdf summary: "
                      "all downloads failed")
                return False
            async with ANSWER_SEM:
                async with _best_effort_typing(message.channel):
                    text = await asyncio.to_thread(
                        pdf_summary.summarize,
                        search.load_persona(self.persona_files),
                        saved, failed, tmpdir,
                        (message.clean_content or "").strip())
            if overflow:
                text += f"\n{pdf_summary.OVERFLOW_NOTE}"
            await deliver_reply(
                message, text, cfg=self.thread_reply_cfg, kind="pdf",
                agent_id=self.agent["id"])
        except Exception as e:
            print(f"[{self.agent['id']}] pdf summary failed: {e}")
            await message.channel.send(
                f"⚠️ PDFの自動要約に失敗したっス: {str(e)[:200]}")
        finally:
            attachments.cleanup(tmpdir)
        return True
