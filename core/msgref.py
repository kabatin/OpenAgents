#!/usr/bin/env python3
"""
Discord メッセージリンク/ID 参照（能力起票 #7）。

利用者が本文に貼った「メッセージへのジャンプリンク」や「素のメッセージID」を
検出し、その投稿の内容・投稿者名・ユーザーID を回答生成の Context に注入する。
（例: 過去メッセージのリンクを貼って「この人のIDが分からない」と聞かれても、
 リンク先を引いて投稿者名とIDを正確に答えられる）

解決は archive.db（全会話を保存済み）を第一とし、DBに無い分だけ呼び出し側が
Discord から best-effort 取得する。ここは副作用の無い純粋処理に責務を絞る
（パース・DB解決・整形）。マーカー方式ではなく、bot 側の正規表現で決定的に
発火させる点は YouTube 要約スキルと同型。

単体テスト: ./venv/bin/python -m unittest test_msgref -v
"""

import re

from core import chat
from core import db

#: どのプラットフォームのリンクとして読むか（実装側が register_links で登録する）
PLATFORM = db.LEGACY_PLATFORM


def _link_re():
    """リンクを読む正規表現。実装が未登録なら何にも一致しないものを返す。"""
    pattern = chat.link_pattern(PLATFORM)
    return pattern if pattern is not None else _NEVER_RE


#: 何にも一致しない正規表現（プラットフォーム未登録時の安全側）
_NEVER_RE = re.compile(r"(?!)")
# メンション/絵文字の <...> は素のID走査の前に除去する（中のIDを誤検出しない）。
ANGLE_RE = re.compile(r"<[^>]*>")
# 素のメッセージID: 17〜20桁のsnowflake単体（前後が数字なら部分一致として除外）。
RAW_ID_RE = re.compile(r"(?<!\d)(\d{17,20})(?!\d)")

MAX_REFS = 5             # 1発言で参照する上限（プロンプト肥大・乱用の抑止）
MAX_CONTENT_LEN = 1000   # 参照本文の最大長（build_history の per_msg に揃える）


def extract_refs(text, guild_id=None):
    """本文から参照先を (channel_id|None, message_id) の列で返す。

    - フルリンクは (channel_id, message_id)。guild_id 指定時は別guildを除外する。
    - 素のIDは channel 不明として (None, message_id)（DB解決は message_id 一意で可）。
    重複排除し、先着順に MAX_REFS 件まで。
    """
    text = text or ""
    refs, seen = [], set()
    for m in _link_re().finditer(text):
        g, c, mid = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if guild_id is not None and g != int(guild_id):
            continue  # 別guildのリンクは参照しない
        if mid not in seen:
            seen.add(mid)
            refs.append((c, mid))
    # 素のID走査はリンク部分と <...> を潰した残りに対して行う
    remainder = ANGLE_RE.sub(" ", _link_re().sub(" ", text))
    for m in RAW_ID_RE.finditer(remainder):
        mid = int(m.group(1))
        if mid not in seen:
            seen.add(mid)
            refs.append((None, mid))
    return refs[:MAX_REFS]


def make_entry(*, message_id, channel_id, channel, author, author_id,
               content, created_at, deleted=False):
    """参照1件の正規化dict。DB解決・Discord取得の両経路で同じ形にする。"""
    return {"message_id": message_id, "channel_id": channel_id,
            "channel": channel, "author": author, "author_id": author_id,
            "content": content or "", "created_at": created_at,
            "deleted": bool(deleted)}


def resolve_from_db(conn, refs):
    """refs の message_id を archive.db から解決し {message_id: entry} を返す。
    見つからないものは含めない（呼び出し側が Discord 取得へフォールバックする）。"""
    out = {}
    for _channel_id, mid in refs:
        if mid in out:
            continue
        row = db.get_message(conn, mid)
        if row is not None:
            out[mid] = make_entry(**row)
    return out


def _jump_link(guild_id, channel_id, message_id):
    return chat.link_to(PLATFORM, guild_id, channel_id, message_id)


def _format_entry(entry, index, guild_id):
    who = entry.get("author") or "不明"
    author_id = entry.get("author_id")
    who_s = f"{who}（ユーザーID: {author_id}）" if author_id else who
    channel = entry.get("channel") or "?"
    date = (entry.get("created_at") or "")[:10]
    deleted = "（削除済み）" if entry.get("deleted") else ""
    content = (entry.get("content") or "").strip() or "（本文なし・添付のみ等）"
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN] + "…"
    link = _jump_link(guild_id, entry.get("channel_id"),
                      entry.get("message_id"))
    return (f"[{index}] (#{channel}, {who_s}, {date}){deleted}\n"
            f"    {content}\n    link: {link}")


def build_reference_block(entries, guild_id):
    """参照メッセージ群を回答生成用のブロックへ整形（無ければ空文字）。
    ヘッダに用途を明記し自己記述的にする（system側の改修を要さない）。"""
    if not entries:
        return ""
    lines = [_format_entry(e, i, guild_id) for i, e in enumerate(entries, 1)]
    return ("【参照メッセージ（利用者がリンク/IDで指定した投稿。投稿者名・"
            "ユーザーID・本文・投稿chをここから正確に引ける）】\n"
            + "\n".join(lines))
