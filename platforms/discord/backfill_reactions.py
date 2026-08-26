#!/usr/bin/env python3
"""過去メッセージのリアクションを Discord API から遡って取り込む（冪等）。

`reactions` テーブルを追加する前の「👍で完結」した会話を埋めるための運用ツール。
以後の分は `reaction_handlers.py` が常時記録するので通常は再実行不要
（障害で取りこぼした期間の復旧にも使える）。

使い方: ./venv/bin/python -m platforms.discord.backfill_reactions

archiver エージェントのトークンで動く。読み取りしか行わない。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from core import config as app_config
from core import db
from core import paths
from core import reminders

API = "https://discord.com/api/v10"
DB_PATH = paths.DB_PATH
SLEEP_SEC = 0.35   # レート制限に優しく


def _token():
    """archiver エージェントのBotトークン。"""
    cfg = app_config.load()
    agent = next((a for a in (cfg.get("agents") or []) if a.get("archiver")),
                 None)
    if agent is None:
        raise RuntimeError("archiver のエージェントが設定されていません")
    token = agent.get("token")
    if not token:
        raise RuntimeError("archiverエージェントのトークンが見つかりません")
    return token


def _get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "OpenAgents backfill"})
    while True:
        try:
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(json.loads(e.read()).get("retry_after", 2))
                time.sleep(wait + 0.5)
                continue
            if e.code in (403, 404):
                return None   # 権限なしch等はスキップ
            raise


def _emoji_key(emoji):
    """保存形式を常時記録側の `str(payload.emoji)` と揃える。
    ここがズレると同じリアクションが二重に入る。"""
    if emoji.get("id"):
        prefix = "a" if emoji.get("animated") else ""
        return f"<{prefix}:{emoji['name']}:{emoji['id']}>"
    return emoji["name"]


def _emoji_param(emoji):
    """reactions APIのパス用表現。"""
    if emoji.get("id"):
        return f"{emoji['name']}:{emoji['id']}"
    return urllib.parse.quote(emoji["name"], safe="")


def backfill(db_path=DB_PATH):
    token = _token()
    now = reminders.fmt(reminders.now_jst())
    with db.connect(db_path) as conn:
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE deleted=0")]
    total_msgs = reacted = saved = 0
    for cid in channels:
        before = ""
        while True:
            page = _get(f"{API}/channels/{cid}/messages?limit=100{before}",
                        token)
            time.sleep(SLEEP_SEC)
            if not page:
                break
            total_msgs += len(page)
            for msg in page:
                if not msg.get("reactions"):
                    continue
                reacted += 1
                for r in msg["reactions"]:
                    users = _get(
                        f"{API}/channels/{cid}/messages/{msg['id']}"
                        f"/reactions/{_emoji_param(r['emoji'])}?limit=100",
                        token) or []
                    time.sleep(SLEEP_SEC)
                    key = _emoji_key(r["emoji"])
                    with db.connect(db_path) as conn:
                        for u in users:
                            db.add_reaction(
                                conn, message_id=int(msg["id"]), emoji=key,
                                user_id=int(u["id"]), created_at=now)
                            saved += 1
            if len(page) < 100:
                break
            before = f"&before={page[-1]['id']}"
        print(f"ch {cid}: 走査済み（累計 msg={total_msgs} "
              f"reacted={reacted} saved={saved}）", flush=True)
    print(f"完了: メッセージ{total_msgs}件走査 / リアクション付き{reacted}件 "
          f"/ 保存{saved}行", flush=True)


if __name__ == "__main__":
    backfill()
