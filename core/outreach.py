#!/usr/bin/env python3
"""週1の御用聞き（RM#52）＋成長の予告（RM#51の素地）。

週1回、ホームchで「何か困ってることないっスか」と軽く声をかける。
直近1週間で新しく使えるようになった能力（デプロイ済み起票）があれば
その告知も添える＝御用聞きと成長の予告を1本にまとめて騒がしくしない。

単体テスト: ./venv/bin/python -m unittest test_batch_pack2 -v
"""

import os
from datetime import timedelta


from core import db
from core import reminders
STATE_PREFIX = "outreach:"
WEEKDAY_DEFAULT = 0    # 月曜
HOUR_DEFAULT = 10
MAX_NEWS = 3


def should_send(db_path, agent_id, *, weekday=WEEKDAY_DEFAULT,
                hour=HOUR_DEFAULT, now=None):
    now = now or reminders.now_jst()
    if now.weekday() != weekday or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 3:
                return False
        except ValueError:
            pass
    return True


def mark_sent(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def recent_capabilities(db_path, now=None):
    """直近1週間でデプロイされた新能力（成長の予告用）。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=7))
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT description FROM capability_requests
               WHERE status='deployed' AND created_at >= ?
               ORDER BY id DESC LIMIT ?""", (since, MAX_NEWS)).fetchall()
    return [r[0] for r in rows]


def build_post(news):
    lines = ["🙋 週の始まりっス！何か困ってることや、"
             "「これ調べといて」みたいなのがあれば気軽に投げてほしいっス〜"]
    if news:
        lines.append("-# ✨ 最近できるようになったこと: "
                     + "／".join(n[:50] for n in news))
    return "\n".join(lines)
