#!/usr/bin/env python3
"""会議前の予習パック（進化ロードマップ#43）。

定例の約1時間前に、前回からの未完了タスク・直近の決定事項・前回議事録への
リンクを1本にまとめて議事録chへ投稿する。集約・整形は決定論（claude不使用＝
定時通知の確実性優先）。既定シャドー（prep.shadow=false で本投稿解禁）。

単体テスト: ./venv/bin/python -m unittest test_prep_pack -v
"""

import os


from core import db
from core import reminders
from core import search
STATE_PREFIX = "prep:"
WEEKDAY_DEFAULT = 4    # 金曜
HOUR_DEFAULT = 19      # 定例開始時刻（この1時間前の周期で送る）
MAX_ITEMS = 10
MAX_DECISIONS = 5


def should_send(db_path, agent_id, *, weekday=WEEKDAY_DEFAULT,
                hour=HOUR_DEFAULT, now=None):
    """定例の1時間前の時間帯で、今週まだ送っていなければ True。"""
    now = now or reminders.now_jst()
    if now.weekday() != weekday or now.hour != hour - 1:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 3:
                return False   # 同じ週の二重送信を防ぐ
        except ValueError:
            pass
    return True


def mark_sent(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def collect(db_path, agent_id):
    """予習パックの材料（未完了タスク・直近決定・前回議事録の位置）。"""
    with db.connect(db_path) as conn:
        open_items = db.open_action_items(conn, agent_id)
        recent_decisions = db.search_decisions(conn, [], limit=MAX_DECISIONS)
        minutes_state = db.get_proactive_state(
            conn, "minutes:" + agent_id)
    return {"open_items": open_items, "decisions": recent_decisions,
            "last_minutes_id": (minutes_state or {}).get(
                "last_checked_message_id") or 0}


def build_pack(data, *, guild_id, minutes_channel_id, today, hour):
    """予習パック本文（純粋関数・載せる中身が無ければ None）。"""
    open_items = data.get("open_items") or []
    decisions_ = data.get("decisions") or []
    if not open_items and not decisions_:
        return None
    lines = [f"📚 今日{hour}時の定例向け・予習パックっス"]
    if open_items:
        lines.append(f"**前回からの未完了タスク（{len(open_items)}件）**")
        for it in open_items[:MAX_ITEMS]:
            mark = "🔴期日超過" if it["due_date"] < today \
                else f"期日 {it['due_date']}"
            lines.append(f"- {it['task'][:60]}（{it['owners']} ／ {mark}）")
        if len(open_items) > MAX_ITEMS:
            lines.append(f"-# 他{len(open_items) - MAX_ITEMS}件は `✅` 済みか"
                         "確認してほしいっス")
    if decisions_:
        lines.append("**最近の決定事項**")
        for d in decisions_:
            topic = f"[{d['topic']}] " if d.get("topic") else ""
            lines.append(f"- {topic}{d['decision'][:60]}")
    if data.get("last_minutes_id"):
        link = search.jump_link(guild_id, minutes_channel_id,
                                data["last_minutes_id"])
        lines.append(f"-# 前回議事録のあたり: {link}")
    return "\n".join(lines)
