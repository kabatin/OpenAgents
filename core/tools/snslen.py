#!/usr/bin/env python3
"""
SNS投稿文字数チェックプラグイン。

マーケ担当がSNS運用で投稿予定のテキストがX（旧Twitter・280字）／Threads（500字）の
文字数制限に収まるかを確認するために使う。ネットワーク接続は不要。
"""

NAME = "snslen"
MARKER = "SNSLEN"
SKILL_NOTE = (
    "【SNS文字数チェックスキル】投稿予定のテキストがX（280字）やThreads（500字）"
    "の文字数制限に収まるか確認したいと頼まれたら、返信本文の最後に改行して"
    "[SNSLEN: 対象のテキスト] を付ける（引数は文字数を数えたい投稿文そのもの）。"
    "文字数確認の依頼以外には付けないこと。"
)

X_LIMIT = 280
THREADS_LIMIT = 500


def count_length(text):
    """テキストの文字数を返す（純粋ロジック・テスト対象）。"""
    return len(text or "")


def judge(length, limit):
    """収まるか(bool)と余裕/超過文字数(超過時は負の値)を返す（純粋ロジック）。"""
    return length <= limit, limit - length


def handle(arg):
    """[SNSLEN: テキスト] の引数を受け、判定結果テキストを返す。"""
    try:
        text = (arg or "").strip()
        if not text:
            return "📏 文字数を数えるテキストを指定してください"
        length = count_length(text)
        x_ok, x_diff = judge(length, X_LIMIT)
        th_ok, th_diff = judge(length, THREADS_LIMIT)
        x_note = f"OK（残り{x_diff}字）" if x_ok else f"NG（{-x_diff}字オーバー）"
        th_note = f"OK（残り{th_diff}字）" if th_ok else f"NG（{-th_diff}字オーバー）"
        return (
            f"📏 文字数: {length}字\n"
            f"X（280字）: {x_note}\n"
            f"Threads（500字）: {th_note}"
        )
    except Exception as e:
        return f"📏 文字数チェックに失敗しました（{e}）"
