#!/usr/bin/env python3
"""停滞プロジェクト検知（進化ロードマップ#41）。

かつて活発だったのに動きが止まったチャンネルを検出し、
「これ止まってません?」と確認する。誤爆（元々静かなch・完了した話）を防ぐため
三重フィルタ: ①直近30日にまとまった発言履歴 ②プロジェクト性の証拠
（納期タスク・宿題・決定のいずれかがそのchに紐づく）③再確認は14日空ける。
既定シャドー（stale_watch.shadow=false で本投稿解禁）。

単体テスト: ./venv/bin/python -m unittest test_batch_pack -v
"""

import os
from datetime import datetime, timedelta


from core import db  # noqa: e402

QUIET_DAYS = 7        # この日数、人間の発言が無ければ停滞候補
HISTORY_MIN = 15      # 停滞前30日間の最低発言数（元々静かなchを除外）
RECHECK_DAYS = 14     # 同じchへの再確認間隔


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def find_stale_channel(db_path, *, exclude_channel_ids=(), home_channel_id=0,
                       now=None):
    """停滞チャンネルを1件返す（無ければ None）。"""
    now = now or datetime.utcnow()
    quiet_cut = _iso(now - timedelta(days=QUIET_DAYS))
    hist_cut = _iso(now - timedelta(days=QUIET_DAYS + 30))
    recheck_cut = (datetime.now() - timedelta(days=RECHECK_DAYS)) \
        .strftime("%Y-%m-%dT%H:%M")
    excl = {int(c) for c in exclude_channel_ids or ()} | {int(home_channel_id)}
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT m.channel_id, c.name, MAX(m.created_at) AS last_at,
                      COUNT(*) AS hist
               FROM messages m
               JOIN users u ON u.id=m.author_id
               LEFT JOIN channels c ON c.id=m.channel_id
               WHERE m.deleted=0 AND u.is_bot=0 AND m.created_at >= ?
               GROUP BY m.channel_id
               HAVING last_at < ? AND hist >= ?""",
            (hist_cut, quiet_cut, HISTORY_MIN)).fetchall()
        for ch_id, name, last_at, hist in rows:
            if ch_id in excl:
                continue
            # プロジェクト性の証拠が無いchは「自然に静かなだけ」とみなす
            proj = conn.execute(
                """SELECT (SELECT COUNT(*) FROM action_items
                            WHERE channel_id=? AND status='open')
                        + (SELECT COUNT(*) FROM decisions
                            WHERE channel_id=? AND status='active')
                        + (SELECT COUNT(*) FROM homework_items
                            WHERE channel_id=? AND status='open')""",
                (ch_id, ch_id, ch_id)).fetchone()[0]
            if not proj:
                continue
            asked = conn.execute(
                """SELECT COUNT(*) FROM proactive_log
                   WHERE kind='stale' AND channel_id=? AND created_at >= ?""",
                (ch_id, recheck_cut)).fetchone()[0]
            if asked:
                continue
            return {"channel_id": ch_id, "channel": name or "?",
                    "last_at": last_at, "history": hist,
                    "open_signals": proj}
    return None


def build_text(ch, quiet_days=QUIET_DAYS):
    return (f"このチャンネル、{quiet_days}日ほど動きが止まってるみたいっスけど、"
            f"状況どうっスか？追跡中の宿題・タスクが{ch['open_signals']}件"
            "残ってるので、完了してたら✅、続いてるなら一言もらえると"
            "安心っス🙋")
