#!/usr/bin/env python3
"""開発BOTの週次開発レポート（進化ロードマップ#60）。

この1週間の開発実績（デプロイ・却下・ロードマップ進捗・見張り状況）を
つむぎが相談室に報告する。集計は決定論（claude不使用）。週1回・時刻ゲート。

単体テスト: ../chatbot/venv/bin/python -m unittest test_dev_report -v
"""

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 同名モジュール衝突を避けるため append（insert(0)禁止＝devbotの既知の罠）
from core import db
STATE_KEY = "devreport:devbot"
WEEKDAY_DEFAULT = 4    # 金曜
HOUR_DEFAULT = 18      # 定例(19時)前に1週間の実績を見せる
MAX_LISTED = 5


def should_send(db_path, *, weekday=WEEKDAY_DEFAULT, hour=HOUR_DEFAULT,
                now=None):
    """指定曜日・時刻台で、今週まだ送っていなければ True。"""
    now = now or datetime.datetime.now()
    if now.weekday() != weekday or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_KEY)
    if state and state.get("last_run_at"):
        try:
            last = datetime.datetime.fromisoformat(state["last_run_at"])
            if (now - last).days < 3:
                return False
        except ValueError:
            pass
    return True


def mark_sent(db_path, now=None):
    now = now or datetime.datetime.now()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_KEY, last_checked_message_id=0,
                               last_run_at=now.isoformat(timespec="seconds"))


def collect(db_path, now=None):
    """レポート材料（この1週間のdev_jobs・ロードマップ・カナリア）。"""
    now = now or datetime.datetime.now()
    since = (now - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        jobs = conn.execute(
            """SELECT cap_req_id, status FROM dev_jobs
               WHERE updated_at >= ? ORDER BY id""", (since,)).fetchall()
        counts = db.roadmap_counts(conn)
        watching = len(db.watching_canaries(conn))
    deployed = [j[0] for j in jobs if j[1] == "deployed"]
    rejected = [j[0] for j in jobs if j[1] in ("rejected", "superseded")]
    failed = [j[0] for j in jobs if j[1] in ("failed", "interrupted")]
    return {"deployed": deployed, "rejected": rejected, "failed": failed,
            "roadmap": counts, "watching": watching}


def _ids(nums):
    shown = "、".join(f"#{n}" for n in nums[:MAX_LISTED])
    return shown + ("…" if len(nums) > MAX_LISTED else "")


def build_report(data):
    """週次レポート本文（つむぎの声・純粋関数）。実績ゼロの週も正直に出す。"""
    rm = data.get("roadmap") or {}
    done = rm.get("done", 0)
    total = sum(rm.values()) or 0
    lines = ["🧵 今週の開発レポートです！"]
    dep = data.get("deployed") or []
    if dep:
        lines.append(f"- デプロイ: {len(dep)}件（起票{_ids(dep)}）🎉")
    else:
        lines.append("- デプロイ: 0件（静かな週でした）")
    if data.get("rejected"):
        lines.append(f"- 見送り/却下: {len(data['rejected'])}件")
    if data.get("failed"):
        lines.append(f"- 失敗/中断: {len(data['failed'])}件"
                     "（続きから再挑戦できます）")
    if total:
        lines.append(f"- 進化ロードマップ: 完了{done}/{total}件"
                     f"（未提案 残り{rm.get('pending', 0)}件）")
    if data.get("watching"):
        lines.append(f"- デプロイ後の見張り中: {data['watching']}件")
    lines.append("-# 新しい機能が欲しくなったら、いつでも起票してくださいね〜✨"
                 "（AI開発室で `!roadmap` も見られます）")
    return "\n".join(lines)
