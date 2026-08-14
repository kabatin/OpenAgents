#!/usr/bin/env python3
"""満足度パルス（進化ロードマップ#18）。

月1回・1問だけの体感アンケート。1️⃣〜5️⃣のリアクション投票で集め、
翌月のパルス投稿で前回の平均を報告する（集計は決定論・claude不使用）。
前回メッセージidは proactive_state（pulse:<agent>）が持つ。

単体テスト: ./venv/bin/python -m unittest test_batch_pack -v
"""

import os


from core import db  # noqa: e402
from core import reminders  # noqa: e402

STATE_PREFIX = "pulse:"
DAY_DEFAULT = 1      # 毎月1日
HOUR_DEFAULT = 11
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def should_send(db_path, agent_id, *, day=DAY_DEFAULT, hour=HOUR_DEFAULT,
                now=None):
    now = now or reminders.now_jst()
    if now.day != day or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 20:
                return False
        except ValueError:
            pass
    return True


def prev_message_id(db_path, agent_id):
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    return (state or {}).get("last_checked_message_id") or 0


def mark_sent(db_path, agent_id, message_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=message_id,
                               last_run_at=reminders.fmt(now))


def tally(counts):
    """{絵文字: 件数} → (平均, 票数)。Bot自身の初期リアクション1票分は引く。"""
    total = votes = 0
    for i, emoji in enumerate(NUMBER_EMOJIS, start=1):
        n = max(0, (counts.get(emoji) or 0) - 1)
        total += i * n
        votes += n
    return ((total / votes) if votes else None, votes)


def build_post(prev_avg, prev_votes, month_label):
    lines = [f"📮 {month_label}の満足度パルスっス！"
             "今月のAIエージェントたちの働き、5段階でどうでしたか？",
             "-# このメッセージに 1️⃣〜5️⃣ のリアクションで投票してほしいっス"
             "（1タップ・匿名じゃないっスけど気軽に）"]
    if prev_votes:
        lines.append(f"-# 先月は 平均⭐{prev_avg:.1f}（{prev_votes}票）でした。"
                     "ありがとうございました！")
    return "\n".join(lines)
