#!/usr/bin/env python3
"""
アーカイブ責務 — メッセージ保存と起動時のbackfill。

archiver（アーカイブ担当）だけが使う。DBへの書込ロジックをbot.pyから切り離し、
「1メッセージの保存」と「未取得分の埋め戻し」の単一責任にまとめる。
db_path は引数で受け取り、この層はグローバル状態に依存しない。
"""

import asyncio

import discord

from core import db


def store_message(conn, message):
    """1メッセージ＋その添付・著者・チャンネルを保存。"""
    db.upsert_channel(
        conn,
        id=message.channel.id,
        name=getattr(message.channel, "name", str(message.channel.id)),
        type=str(message.channel.type),
        parent_id=getattr(getattr(message.channel, "parent", None), "id", None),
    )
    db.upsert_user(
        conn,
        id=message.author.id,
        name=str(message.author),
        display_name=message.author.display_name,
        is_bot=message.author.bot,
    )
    db.insert_message(
        conn,
        id=message.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        content=message.content,
        created_at=message.created_at.isoformat(),
        edited_at=message.edited_at.isoformat() if message.edited_at else None,
        reply_to=(message.reference.message_id if message.reference else None),
    )
    for att in message.attachments:
        ct = (att.content_type or "").lower()
        db.insert_attachment(
            conn,
            id=att.id,
            message_id=message.id,
            filename=att.filename,
            content_type=att.content_type,
            size=att.size,
            is_image=ct.startswith("image/"),
            is_video=ct.startswith("video/"),
        )


async def backfill(guild, db_path):
    """各チャンネルの保存済み最大ID以降を取得して埋める。"""
    text_channels = [c for c in guild.channels
                     if isinstance(c, (discord.TextChannel, discord.Thread))]
    # アクティブスレッドも対象に含める
    for c in guild.channels:
        if isinstance(c, discord.TextChannel):
            text_channels.extend(c.threads)

    total = 0
    for channel in text_channels:
        perms = channel.permissions_for(guild.me)
        if not perms.read_message_history:
            print(f"skip (no history perm): #{channel}")
            continue

        with db.connect(db_path) as conn:
            after_id = db.last_message_id(conn, channel.id)
        after = discord.Object(id=after_id) if after_id else None

        count = 0
        try:
            # 履歴はネットワークawaitを跨ぐので、DBロックを保持したまま待たない。
            # バッファに溜めて短いトランザクションで書く（live書込を長時間
            # ブロックしない・イベントループを詰まらせない）
            buf = []

            def _flush(rows):
                if not rows:
                    return
                with db.connect(db_path) as conn:
                    for msg in rows:
                        store_message(conn, msg)

            async for message in channel.history(limit=None, after=after,
                                                 oldest_first=True):
                buf.append(message)
                count += 1
                if len(buf) >= 100:
                    await asyncio.to_thread(_flush, buf)
                    buf = []
            await asyncio.to_thread(_flush, buf)
        except discord.Forbidden:
            print(f"skip (forbidden): #{channel}")
            continue
        except Exception as e:
            print(f"error backfilling #{channel}: {e}")
            continue

        total += count
        if count:
            print(f"backfilled #{channel}: +{count}")

    print(f"backfill done: +{total} messages")
