#!/usr/bin/env python3
"""
チャンネル文脈要約（エージェントv2 Phase 0）。設計: docs/agents-v2-design.md §3.1

連続性を生ログではなく「要約1本」で担保する。要約は archive.db の
thread_summaries に置き、Contextは毎回組み立てる（覚えているのではなく、
毎回思い出す）。更新は応答後に非同期で行い、差分が小さいうちはスキップして
claude 呼び出しを節約する。
"""

import os
from datetime import datetime

from core import invoke_claude
from core import db
# これ未満の新規発言では要約を更新しない（応答のたびのCLI呼び出しを防ぐ）
MIN_NEW_MESSAGES = 6
# 1回の更新で読む新規発言の上限
MAX_MESSAGES_PER_UPDATE = 80
# 要約の目標文字数（プロンプトで指示）
SUMMARY_MAX_CHARS = 600
# 要約生成のタイムアウト（短め・定型処理）
UPDATE_TIMEOUT_SEC = 180
# 1発言あたりの引用上限（長文貼り付けで要約プロンプトが肥大しないように）
PER_MESSAGE_CHARS = 400


def format_message_lines(rows):
    """messages_after() の結果を要約プロンプト用の行群に整形（純粋関数）。"""
    lines = []
    for r in rows:
        text = (r.get("content") or "").strip()
        if not text:
            continue
        if len(text) > PER_MESSAGE_CHARS:
            text = text[:PER_MESSAGE_CHARS] + "…"
        date = (r.get("created_at") or "")[:10]
        lines.append(f"[{date}] {r.get('author') or '?'}: {text}")
    return lines


def build_update_prompt(prev_summary, lines):
    """既存要約＋新規発言から要約更新プロンプトを組み立てる（純粋関数）。"""
    prev_block = (prev_summary or "").strip() or "（まだ要約は無い）"
    return (
        "あなたはチームのチャンネルの書記です。既存の要約と新しい発言を"
        "統合して、このチャンネルの「現在の文脈要約」を更新してください。\n"
        f"- 全体を{SUMMARY_MAX_CHARS}文字以内、箇条書き中心で\n"
        "- 進行中の話題 / 決定事項 / 未解決の依頼 / 重要な事実を優先する\n"
        "- 古くなった・解決済みの話題は圧縮または削除してよい\n"
        "- 要約本文のみを出力する（前置き・後書き不要）\n\n"
        f"【既存の要約】\n{prev_block}\n\n"
        "【新しい発言（古い順）】\n" + "\n".join(lines)
    )


def get_summary_text(db_path, channel_id):
    """Context注入用の要約テキスト。無ければ空文字。"""
    try:
        with db.connect(db_path) as conn:
            row = db.get_thread_summary(conn, channel_id)
        return (row or {}).get("summary") or ""
    except Exception:
        return ""


def maybe_update(db_path, channel_id, model=invoke_claude.DEFAULT_MODEL,
                 invoke_fn=None):
    """差分が十分あれば要約を更新する（同期・ブロッキング）。

    Returns: 更新後の要約 / スキップ時は None。
    invoke_fn はテスト用の差し替え口（prompt -> text）。
    """
    with db.connect(db_path) as conn:
        prev = db.get_thread_summary(conn, channel_id)
        if prev is None:
            # コールドスタート: 最古から辿ると履歴の長いchで「現在の文脈」に
            # いつまでも追いつかないため、初回は最新N件から要約を立ち上げる
            rows = db.latest_messages(conn, channel_id,
                                      limit=MAX_MESSAGES_PER_UPDATE)
        else:
            rows = db.messages_after(conn, channel_id,
                                     after_id=prev.get("covered_until"),
                                     limit=MAX_MESSAGES_PER_UPDATE)
    lines = format_message_lines(rows)
    if len(lines) < MIN_NEW_MESSAGES:
        return None
    prompt = build_update_prompt((prev or {}).get("summary"), lines)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=UPDATE_TIMEOUT_SEC).text)
    summary = (fn(prompt) or "").strip()
    if not summary:
        return None
    covered_until = rows[-1]["id"]
    with db.connect(db_path) as conn:
        db.upsert_thread_summary(
            conn,
            channel_id=channel_id,
            summary=summary,
            covered_until=covered_until,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
    return summary
