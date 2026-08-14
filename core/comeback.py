#!/usr/bin/env python3
"""浦島パック（とっておき#102）。

一定期間（既定5日）発言のなかったメンバーが戻ってきたのを検知したら、
不在中に起きたこと（新しい決定・完了・イベント・その人宛のopenタスク）を
1本にまとめて、復帰の発言へのリプライで渡す。本人にだけ・復帰につき1回だけ。

材料は全部既存の構造化データ＝集約は決定論（claude不使用・エピソード記憶の
配達係）。#54不在サポート（慎重枠）の安全化版: 不在中の代行はせず、
帰ってきた本人に要約を渡すだけなので実害ゼロ。

単体テスト: ./venv/bin/python -m unittest test_treasure_pack -v
"""

import os
from datetime import timedelta


from core import db
from core import reminders
SCAN_KEY = "comebackscan:"
WELCOME_KEY = "comeback:"
MIN_DAYS_DEFAULT = 5
MAX_LINES = 8
COOLDOWN_HOURS = 24


def _parse_ts(ts):
    """messagesのcreated_at（ISO/スペース区切り混在）を素朴にdatetime化。"""
    from datetime import datetime
    s = (ts or "").replace(" ", "T")[:16]
    return datetime.strptime(s, "%Y-%m-%dT%H:%M")


def detect(db_path, agent_id, exclude_channel_ids=(), min_days=MIN_DAYS_DEFAULT):
    """前回チェック以降の人間の発言から「復帰者」を検出（決定論）。
    Returns: [{"user_id","message_id","channel_id","away_since","days"}]"""
    out = []
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, SCAN_KEY + agent_id)
        last_id = (state or {}).get("last_checked_message_id") or 0
        if last_id == 0:
            # 初回は現在位置まで送るだけ（過去ログに遡って反応しない）
            row = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages"
                               ).fetchone()
            db.set_proactive_state(
                conn, SCAN_KEY + agent_id, last_checked_message_id=row[0],
                last_run_at=reminders.fmt(reminders.now_jst()))
            return []
        rows = conn.execute(
            """SELECT m.id, m.author_id, m.channel_id, m.created_at
               FROM messages m JOIN users u ON u.id = m.author_id
               WHERE m.id > ? AND m.deleted=0 AND u.is_bot=0
               ORDER BY m.id ASC LIMIT 200""", (last_id,)).fetchall()
        if not rows:
            return []
        seen_users = set()
        for mid, uid, ch, _created in rows:
            if uid in seen_users or ch in exclude_channel_ids:
                continue
            seen_users.add(uid)
            gap = db.user_prev_message_gap(conn, uid, mid)
            if gap is None:
                continue
            try:
                prev_at, cur_at = _parse_ts(gap[0]), _parse_ts(gap[1])
            except ValueError:
                continue
            days = (cur_at - prev_at).days
            if days < min_days:
                continue
            wstate = db.get_proactive_state(conn, WELCOME_KEY + str(uid))
            if wstate and wstate.get("last_run_at"):
                try:
                    last_w = reminders.parse_dt(wstate["last_run_at"])
                    if (reminders.now_jst() - last_w
                            < timedelta(hours=COOLDOWN_HOURS)):
                        continue
                except ValueError:
                    pass
            out.append({"user_id": uid, "message_id": mid, "channel_id": ch,
                        "away_since": reminders.fmt(prev_at), "days": days})
        db.set_proactive_state(
            conn, SCAN_KEY + agent_id, last_checked_message_id=rows[-1][0],
            last_run_at=reminders.fmt(reminders.now_jst()))
    return out


def build_digest(db_path, user_id, away_since):
    """不在中に起きたことの要約（決定論・純粋な集約）。無ければ None。"""
    lines = []
    with db.connect(db_path) as conn:
        for (t,) in conn.execute(
                """SELECT decision FROM decisions
                   WHERE status='active' AND created_at >= ?
                   ORDER BY id DESC LIMIT 5""", (away_since,)).fetchall():
            lines.append(f"- [決定] {t[:80]}")
        for (t, d) in conn.execute(
                """SELECT task, due_date FROM action_items
                   WHERE status='open' AND owners LIKE ?
                   ORDER BY due_date ASC LIMIT 3""",
                (f"%<@{user_id}>%",)).fetchall():
            lines.append(f"- [あなた宛タスク] {t[:60]}（期日{d}）")
        for (t, d) in conn.execute(
                """SELECT name, event_date FROM events
                   WHERE status='planned' AND created_at >= ?
                   ORDER BY event_date ASC LIMIT 3""",
                (away_since,)).fetchall():
            lines.append(f"- [イベント] {t[:60]}（{d}）")
    if not lines:
        return None
    return lines[:MAX_LINES]


def build_post(days, lines):
    return (f"おかえりなさいっス！{days}日ぶりっスね🙌 "
            "留守の間にあったことをまとめておいたっス:\n"
            + "\n".join(lines)
            + "\n-# 詳しく知りたいものがあれば聞いてほしいっス"
              "（この案内は復帰時に1回だけっス）")


def mark_welcomed(db_path, user_id):
    with db.connect(db_path) as conn:
        db.set_proactive_state(
            conn, WELCOME_KEY + str(user_id), last_checked_message_id=0,
            last_run_at=reminders.fmt(reminders.now_jst()))
