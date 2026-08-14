#!/usr/bin/env python3
"""会話セッションの持続（resume方式・アーカイブ担当カナリア 2026-08-03）。

OpenClaw時代の「文脈理解の強さ」を、プロセス使い捨てのまま取り戻す層。
claude CLI の --resume で (エージェント, チャンネル) 単位の会話セッションを
継続する。要点は**有界性**:

  - ホットウィンドウ: 前回のやり取りから hot_minutes 以内だけresume
    （冷えた会話は新規セッション＝従来のDB注入がコールドスタートを支える）
  - ターン上限・世代上限: max_turns / max_age_hours で強制世代交代
    （不死身の状態が腐るOpenClawの病気を構造的に防ぐ）
  - resume失敗は新規セッションへ静かに退化（誠実な失敗）
  - 添付ターンは対象外（cwdが一時dirになりセッションのcwdスコープと合わない）

安全弁（マーカー実行・ゲート・枠）は全てコード側にあり、この層の影響を
受けない。config の session_resume.enabled で即オフ可能。

単体テスト: ./venv/bin/python -m unittest test_sessions -v
"""

import os

from core import db
from core import paths
from core import reminders

HOT_MINUTES_DEFAULT = 60
MAX_TURNS_DEFAULT = 20
MAX_AGE_HOURS_DEFAULT = 6
# セッション呼び出しの固定cwd（resumeはcwdスコープのため常に同じ場所で起動）
SESSION_CWD = paths.SESSION_CWD


def should_resume(row, cfg, now=None):
    """保存済みセッションをresumeすべきか（純粋関数）。"""
    if not row or not row.get("session_id"):
        return False
    now = now or reminders.now_jst()
    hot = int(cfg.get("hot_minutes", HOT_MINUTES_DEFAULT))
    max_turns = int(cfg.get("max_turns", MAX_TURNS_DEFAULT))
    max_age = int(cfg.get("max_age_hours", MAX_AGE_HOURS_DEFAULT))
    try:
        last = reminders.parse_dt(row["last_used_at"])
        started = reminders.parse_dt(row["started_at"])
    except (ValueError, TypeError):
        return False
    if (now - last).total_seconds() > hot * 60:
        return False          # 冷えた会話は新規（DB注入が支える）
    if row.get("turns", 0) >= max_turns:
        return False          # ターン上限で世代交代
    if (now - started).total_seconds() > max_age * 3600:
        return False          # 世代上限
    return True


def resume_id(db_path, agent_id, channel_id, cfg, now=None):
    """resumeするsession_id（無ければ None＝新規開始）。"""
    with db.connect(db_path) as conn:
        row = db.get_agent_session(conn, agent_id, channel_id)
    return row["session_id"] if should_resume(row, cfg, now) else None


def record_use(db_path, agent_id, channel_id, session_id, *, resumed,
               now=None):
    """呼び出し成功後の記録。resumed=False は新規世代の開始。"""
    if not session_id:
        return
    now = now or reminders.now_jst()
    ts = reminders.fmt(now)
    with db.connect(db_path) as conn:
        prev = db.get_agent_session(conn, agent_id, channel_id)
        if resumed and prev and prev["session_id"] == session_id:
            turns = (prev["turns"] or 0) + 1
            started = prev["started_at"]
        else:
            turns = 1
            started = ts
        db.save_agent_session(
            conn, agent_id=agent_id, channel_id=channel_id,
            session_id=session_id, turns=turns, started_at=started,
            last_used_at=ts)


def clear(db_path, agent_id, channel_id):
    """壊れたセッションの破棄（次ターンから新規世代）。"""
    with db.connect(db_path) as conn:
        db.clear_agent_session(conn, agent_id, channel_id)
