#!/usr/bin/env python3
"""
Discord Webhook に議事録を投稿（ゲートウェイ非依存・標準ライブラリのみ）

- bot.py から   : `await post_to_discord(text)` で使う
- heartbeat/手動 : `python3 discord_notifier.py <議事録ファイル>` で投稿
                   （引数なしなら標準入力から読む）

requests 等の外部依存を使わず urllib のみで実装しているため、
venv が無い素の python3（heartbeat 経路）からでもそのまま実行できる。
"""

import os
import sys
import json
import asyncio
import urllib.request
import urllib.error

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
# Discord の 1 メッセージあたりの文字数上限
DISCORD_MSG_LIMIT = 2000
# urllib のデフォルト UA (Python-urllib/x.y) は Cloudflare に弾かれる(403 error 1010)。
# Discord 規定の User-Agent を付ける。
USER_AGENT = 'DiscordBot (https://github.com/openagents/meetingbot, 1.0)'


def _load_webhook_url():
    """Discord Webhook URL を取得する（環境変数を正とし、config.json はフォールバック）。"""
    url = os.environ.get('MEETINGBOT_WEBHOOK_URL')
    if url:
        return url
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)
    url = config.get('discord_webhook_url')
    if not url:
        raise RuntimeError(
            "MEETINGBOT_WEBHOOK_URL 環境変数（または config.json の discord_webhook_url）が"
            "設定されていません"
        )
    return url


def _split_message(text, limit=DISCORD_MSG_LIMIT):
    """Discord の文字数上限に収まるよう行単位で分割する。

    1 行が単体で上限を超える場合のみ、その行を強制的に分割する。
    """
    chunks = []
    current = ''
    for line in text.split('\n'):
        # 1 行が上限を超える場合は強制分割
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ''
            chunks.append(line[:limit])
            line = line[limit:]
        # +1 は改行分
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _post(url, text):
    """Discord Webhook に投稿（同期・ブロッキング）。失敗時は例外を送出する。"""
    for chunk in _split_message(text):
        payload = json.dumps({'content': chunk}).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': USER_AGENT,
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            raise RuntimeError(
                f"Discord Webhook 投稿失敗 (status={e.code}): {body[:300]}"
            )


async def post_to_discord(minutes):
    """議事録を Discord Webhook に投稿する（失敗時は例外を送出する）。

    Args:
        minutes: 議事録テキスト（Markdown。Discord がそのまま解釈する）
    """
    url = _load_webhook_url()
    print("📤 Discord Webhook に投稿中...")
    # ブロッキングな HTTP 呼び出しは別スレッドで実行し、イベントループを止めない
    await asyncio.to_thread(_post, url, minutes)
    print("✅ Discord への投稿が完了しました")


def main():
    """CLI: ファイル（引数）または標準入力の内容を Discord に投稿する。"""
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    text = text.strip()
    if not text:
        print("⚠️ 投稿する内容が空です")
        sys.exit(1)

    _post(_load_webhook_url(), text)
    print("✅ Discord への投稿が完了しました")


if __name__ == '__main__':
    main()
