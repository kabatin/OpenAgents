#!/usr/bin/env python3
"""機密（パスワード等）を含む投稿の本文を archive.db から消す。

    ./venv/bin/python -m core.redact_message 1234567890123456789 [ID…]        # 対象の確認だけ
    ./venv/bin/python -m core.redact_message 1234567890123456789 [ID…] --yes  # 実行

やること: 本文を定型文に置き換え、検索の索引から外し、DBファイル上の残骸も消す。
やらないこと: チャット上（Discord等）の投稿の削除（投稿者か管理者が手で消す）と、
漏れたパスワードの変更。**DBから消しても、パスワードは変えるまで有効**。

元に戻せない。本文は表示しない（端末の履歴に機密を残さないため）。
"""

import argparse
import os
import re
import sqlite3
import sys

from core import db
from core import paths

DB_PATH = paths.DB_PATH

# 残骸の確認に使う「機密らしい並び」。英数字記号が6字以上続くもの
_TOKEN_RE = re.compile(r"[!-~]{6,}")


def describe(db_path, ids):
    """対象の一覧（本文は返さない）。[{id, found, channel, author, date, chars, state}]"""
    out = []
    with db.connect(db_path) as conn:
        for mid in ids:
            m = db.get_message(conn, mid)
            if m is None:
                out.append({"id": mid, "found": False})
                continue
            raw = conn.execute("SELECT deleted FROM messages WHERE id=?",
                               (mid,)).fetchone()[0]
            state = ("墨消し済み" if raw == db.DELETED_REDACTED
                     else "削除済み（本文は残っている）" if raw else "公開中")
            out.append({"id": mid, "found": True, "channel": m["channel"],
                        "author": m["author"], "date": (m["created_at"] or "")[:10],
                        "chars": len(m["content"] or ""), "state": state})
    return out


def _leftovers(db_path, tokens):
    """DBファイルとWALに、まだバイト列として残っている並びの数。"""
    blob = b""
    for path in (db_path, db_path + "-wal"):
        if os.path.exists(path):
            with open(path, "rb") as f:
                blob += f.read()
    return sum(1 for t in tokens if t.encode("utf-8") in blob)


def _also_elsewhere(conn, token, ids):
    """消す投稿の他にも、同じ並びを含む投稿があるか。"""
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT 1 FROM messages WHERE id NOT IN ({marks}) "
        "AND instr(content, ?) > 0 LIMIT 1", (*ids, token)).fetchone() is not None


def redact(db_path, ids):
    """墨消しを実行して結果を返す。
    redacted: 消したID / unique: 消した投稿にしか無かった並びの数 /
    leftovers: そのうちファイルにまだ残っている数（0が正常）/
    shared: 他の投稿にもある並びの数（メールアドレス等。消していない）/
    checkpoint_busy: WALを空にできなかった"""
    ids = list(ids)
    tokens, done = set(), []
    with db.connect(db_path) as conn:
        # 消した領域をゼロで上書きさせる。無いと本文が空きページに残り、
        # ファイルを直接読めば取り出せる
        conn.execute("PRAGMA secure_delete=ON")
        for mid in ids:
            row = conn.execute("SELECT content FROM messages WHERE id=?",
                               (mid,)).fetchone()
            if row is None:
                continue
            found = set(_TOKEN_RE.findall(row[0] or ""))
            if db.redact_message(conn, mid):
                tokens |= found
                done.append(mid)
        shared = {t for t in tokens if _also_elsewhere(conn, t, ids)}
        if done:
            # 索引の 'delete' は墓標を足すだけで、語は古い区画に残る。
            # 区画をまとめ直して実際に消す
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
    # WALに残った旧ページを本体へ流してWALを空にする（別プロセスが読んでいる間は
    # 空にできないことがある。その場合は busy として返す）
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        busy = bool(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
    finally:
        conn.close()
    unique = tokens - shared
    return {"redacted": done, "unique": len(unique), "shared": len(shared),
            "leftovers": _leftovers(db_path, unique), "checkpoint_busy": busy}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="+", type=int, help="メッセージID")
    ap.add_argument("--yes", action="store_true", help="実行する（無ければ確認だけ）")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args(argv)

    for d in describe(args.db, args.ids):
        if not d["found"]:
            print(f"{d['id']}: DBに無い")
            continue
        print(f"{d['id']}: #{d['channel']} {d['author']} {d['date']} "
              f"{d['chars']}字 [{d['state']}]")
    if not args.yes:
        print("確認だけ。実行するには --yes を付ける（元に戻せない）")
        return 0

    res = redact(args.db, args.ids)
    print(f"墨消し: {len(res['redacted'])}件")
    if res["checkpoint_busy"]:
        print("注意: WALを空にできなかった（BOTが読んでいる）。BOT再起動後にもう一度実行")
    if res["shared"]:
        print(f"他の投稿にもある文字列: {res['shared']}個（メールアドレス等。消していない。"
              "機密そのものが他の投稿にもあるなら、そのIDも墨消しする）")
    if res["leftovers"]:
        print(f"注意: この投稿にしか無かった文字列{res['unique']}個のうち"
              f"{res['leftovers']}個がファイルに残っている")
        return 1
    print(f"この投稿にしか無かった文字列{res['unique']}個は、DBファイルから消えた。"
          "チャット上の投稿の削除とパスワード変更は別途必要")
    return 0


if __name__ == "__main__":
    sys.exit(main())
