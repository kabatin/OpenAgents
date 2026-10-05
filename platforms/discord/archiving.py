#!/usr/bin/env python3
"""
アーカイブ責務 — メッセージ保存と起動時のbackfill。

archiver（アーカイブ担当）だけが使う。DBへの書込ロジックをbot.pyから切り離し、
「1メッセージの保存」と「未取得分の埋め戻し」の単一責任にまとめる。
db_path は引数で受け取り、この層はグローバル状態に依存しない。
"""

import asyncio

import discord

from core import archive_window
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


# 大きなサーバーでは取り込みが長時間かかる。進んでいることをログで見せる間隔
PROGRESS_EVERY = 5000


async def _pull(channel, db_path, **history_kwargs):
    """履歴を取りながら短いトランザクションで保存する。保存した件数を返す。
    履歴はネットワークawaitを跨ぐので、DBロックを保持したまま待たない。
    バッファに溜めて短いトランザクションで書く（live書込を長時間
    ブロックしない・イベントループを詰まらせない）。"""
    def _flush(rows):
        if not rows:
            return
        with db.connect(db_path) as conn:
            for msg in rows:
                store_message(conn, msg)

    buf, count = [], 0
    async for message in channel.history(limit=None, **history_kwargs):
        buf.append(message)
        count += 1
        if len(buf) >= 100:
            await asyncio.to_thread(_flush, buf)
            buf = []
        if count % PROGRESS_EVERY == 0:
            print(f"backfilling #{channel}: {count} 件…", flush=True)
    await asyncio.to_thread(_flush, buf)
    return count


async def backfill_channel(channel, me, db_path, since):
    """1チャンネルを埋める。since（UTC datetime / None=すべて）より前は取らない。

    1) 前回の続き: 保存済みの最新と起点の新しい方より後（停止中の取りこぼし）
    2) 古い方: 起点を前にずらした・初めて範囲を確かめる、ときだけ
       保存済みの最古より前を起点まで遡る
    """
    since_sf = discord.utils.time_snowflake(since) if since else None
    with db.connect(db_path) as conn:
        last_id = db.last_message_id(conn, channel.id)
        first_id = db.first_message_id(conn, channel.id)
        coverage = db.get_archive_coverage(conn, channel.id)

    start = max((x for x in (last_id, since_sf) if x), default=None)
    count = await _pull(channel, db_path,
                        after=discord.Object(id=start) if start else None,
                        oldest_first=True)
    if first_id is not None and archive_window.needs_older(coverage, since):
        count += await _pull(channel, db_path,
                             before=discord.Object(id=first_id),
                             after=discord.Object(id=since_sf) if since_sf else None,
                             oldest_first=False)
    with db.connect(db_path) as conn:
        db.set_archive_coverage(conn, channel.id, archive_window.since_key(since))
    return count


async def backfill(guild, db_path, since=None):
    """全チャンネルを埋める（設定 archive.since の起点より前は取りに行かない）。"""
    text_channels = [c for c in guild.channels
                     if isinstance(c, (discord.TextChannel, discord.Thread))]
    # アクティブスレッドも対象に含める
    for c in guild.channels:
        if isinstance(c, discord.TextChannel):
            text_channels.extend(c.threads)

    if since is not None:
        print(f"backfill: {since.isoformat()} より前の会話は取り込みません"
              "（設定 archive.since）")
    total = 0
    for channel in text_channels:
        perms = channel.permissions_for(guild.me)
        if not perms.read_message_history:
            print(f"skip (no history perm): #{channel}")
            continue
        try:
            count = await backfill_channel(channel, guild.me, db_path, since)
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
