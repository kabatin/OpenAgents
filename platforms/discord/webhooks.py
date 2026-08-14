#!/usr/bin/env python3
"""
Webhook人格の投稿プリミティブ（エージェントv2 Phase 2）。設計: §7 二階層モデル

Webhook人格（試用枠）は自前のBotアカウントを持たず、チャンネルのWebhookを
使って「名前・アイコンをメッセージ単位で差し替えて」投稿する。表示名・アイコンは
agents台帳の値をコードが強制注入し、LLMには選ばせない（なりすまし防止）。

- 受信はアーカイブ担当クライアント（archiver・全ch受信）を流用するので、ここは投稿専用。
- チャンネルごとに1つWebhookを取得/作成してキャッシュ（毎回作らない）。
  作成は per-channel ロックで直列化し、Webhookの重複作成を防ぐ。
- 削除済みキャッシュ(NotFound)は捨てて1度だけ作り直してリトライする。
"""

import asyncio

import discord

WEBHOOK_NAME = "agent1-agents"     # 我々が作るWebhookの識別名（他Webhookと区別）
MAX_CHUNK = 1900                  # Discordの2000字制限に対する分割単位

_locks = {}                       # channel_id -> asyncio.Lock（作成の直列化）


def _lock_for(channel_id):
    lk = _locks.get(channel_id)
    if lk is None:
        lk = _locks[channel_id] = asyncio.Lock()
    return lk


async def _get_or_create_webhook(channel, cache, avatar_bytes=None):
    """チャンネルのWebhookを取得（無ければ作成）してキャッシュする。
    作成をロックで直列化し、同時アクセスでの重複作成を防ぐ。
    avatar_bytes を渡すと、そのチャンネルのWebhook自体のアイコンに設定する
    （ローカル画像用。Discordは per-message の avatar_url に URL しか受けないため、
    ローカルアイコンは Webhook 本体に載せる。home_channel は人格と1:1）。
    Manage Webhooks 権限が必要。"""
    if channel.id in cache:
        return cache[channel.id]
    async with _lock_for(channel.id):
        if channel.id in cache:            # ロック取得までに他が作っていれば再利用
            return cache[channel.id]
        # 既存Webhookの列挙が一時失敗しただけで create に進むと同名Webhookが
        # 増殖する（ch上限に到達すると作成不能に）。列挙失敗時は作成せず伝播。
        found = None
        for wh in await channel.webhooks():
            if wh.name == WEBHOOK_NAME and wh.token:
                found = wh
                break
        hook = found
        if hook is None:
            hook = await channel.create_webhook(
                name=WEBHOOK_NAME, avatar=avatar_bytes)
        elif avatar_bytes and hook.avatar is None:
            # 既存だがアイコン未設定なら一度だけ載せる
            try:
                await hook.edit(avatar=avatar_bytes)
            except Exception:
                pass
        cache[channel.id] = hook
        return hook


async def post_as_persona(channel, *, name, content, cache, avatar_url=None,
                          avatar_bytes=None, allowed_mentions=None, wait=False):
    """Webでpersona名・アイコンを差し替えて投稿する（2000字で分割）。
    name は台帳の値をそのまま渡すこと（コード側で強制する前提）。
    アイコンは avatar_url（http URL・per-message）または avatar_bytes
    （ローカル画像・Webhook本体）のどちらかで指定する。
    キャッシュしたWebhookが削除済み(NotFound)なら捨てて1度だけ作り直す。
    wait=True で最後に送ったメッセージを返す（承認リアクション紐付け用）。"""
    kwargs = {"username": name}
    # per-message の avatar_url は http(s) URL のみ有効。ローカルパスは
    # Webhook本体のアイコン(avatar_bytes)で扱うため、ここでは渡さない
    if avatar_url and str(avatar_url).startswith(("http://", "https://")):
        kwargs["avatar_url"] = avatar_url
    if allowed_mentions is not None:
        kwargs["allowed_mentions"] = allowed_mentions
    text = content or "…"
    chunks = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
    for attempt in (1, 2):
        hook = await _get_or_create_webhook(channel, cache, avatar_bytes)
        try:
            last = None
            for chunk in chunks:
                last = await hook.send(chunk, wait=wait, **kwargs)
            return last
        except discord.NotFound:
            cache.pop(channel.id, None)    # stale → 捨てて作り直し1回だけ再試行
            if attempt == 2:
                raise


def validate_persona_name(name, *, existing_names):
    """なりすまし防止: 空・既存メンバー/Bot/人格と紛らわしい名前を弾く。
    existing_names は小文字化済みの既存表示名集合。問題があれば理由文字列、
    無ければ None を返す。"""
    n = (name or "").strip()
    if not n:
        return "名前が空です"
    if len(n) > 32:
        return "名前が長すぎます（32文字以内）"
    if n.lower() in existing_names:
        return f"「{n}」は既存のメンバー/Botと紛らわしいので使えません"
    return None
