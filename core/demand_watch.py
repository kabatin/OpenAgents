#!/usr/bin/env python3
"""需要検知spawn提案（RM#66）＋期間限定人格（RM#67）。

月次で「役割の空白」を探し、AI人事の採用フロー（hr.py／pending_hires）に
乗せて提案する。判断は人間（👍で採用実行）＝提案と実行の分離は不変。

  #66: 未回答質問・救済ログ・起票の内容から繰り返し出る話題領域を検出し、
       既存3体の守備範囲外なら「担当を置くべきかも」と提案
  #67: 開催が近いイベント（events.planned）があれば、期間限定の専属人格を
       提案する（retire予定日を役割文に明記＝終わったら人間がfireする）

単体テスト: ./venv/bin/python -m unittest test_batch_pack2 -v
"""

import json
import os
import re
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "demand:"
DAY_DEFAULT = 3
HOUR_DEFAULT = 11
MIN_SIGNALS = 6            # 材料が少ないうちは走らない
EVENT_SOON_DAYS = 45       # この日数以内のイベントは専属人格の候補
DETECT_TIMEOUT_SEC = 180
_JSON_RE = re.compile(r"\{.*\}", re.S)


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


def mark_sent(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def collect_signals(db_path, now=None):
    """役割の空白を示す材料（救済対象の質問・起票の内容）。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=60))
    out = []
    with db.connect(db_path) as conn:
        for (detail,) in conn.execute(
                """SELECT detail FROM proactive_log
                   WHERE kind IN ('rescue','none') AND detail IS NOT NULL
                     AND created_at >= ?""", (since,)).fetchall():
            if detail and len(detail) > 10:
                out.append(detail[:120])
        for (desc,) in conn.execute(
                """SELECT description FROM capability_requests
                   WHERE created_at >= ?""", (since,)).fetchall():
            out.append((desc or "")[:120])
    return [s for s in out if s.strip()]


def upcoming_events(db_path, now=None):
    """開催が近い承認済みイベント（#67の候補）。"""
    now = now or reminders.now_jst()
    today = now.strftime("%Y-%m-%d")
    limit = (now + timedelta(days=EVENT_SOON_DAYS)).strftime("%Y-%m-%d")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT name, event_date FROM events
               WHERE status='planned' AND event_date >= ? AND event_date <= ?
               ORDER BY event_date ASC LIMIT 3""", (today, limit)).fetchall()
    return [{"name": r[0], "event_date": r[1]} for r in rows]


def build_prompt(signals, roster, events):
    listing = "\n".join(f"- {s}" for s in signals[:40])
    roster_s = "\n".join(f"- {n}: {r}" for n, r in roster)
    ev = "\n".join(f"- {e['name'][:50]}（{e['event_date']}）"
                   for e in events) or "（近日のイベントなし）"
    return (
        "社内AIチームに「担当が居ない役割」があるかを分析して。\n\n"
        f"【現メンバーと担当】\n{roster_s}\n\n"
        f"【最近くり返し出ている話題・未対応の依頼】\n{listing}\n\n"
        f"【開催が近いイベント】\n{ev}\n\n"
        "ルール:\n"
        "- 現メンバーの担当で自然にカバーできるものは提案しない\n"
        "- 同じ領域の質問・依頼が複数回出ているときだけ提案する\n"
        "- 近日イベントがあり、専属で世話役が居ると明確に良い場合は"
        "期間限定の人格を提案してよい（その場合 temporary=true・"
        "role文の末尾に「（〇月〇日のイベント終了までの期間限定）」を入れる）\n"
        "- 提案は最大1件。無ければ空。迷ったら提案しない（増殖は慎重に）\n"
        '- 出力はJSONのみ: {"proposal": {"id": "keiri", "name": "AI経理", '
        '"role": "経理・支払い関連の相談担当", "channel_name": "経理相談", '
        '"temporary": false, "reason": "経理系の質問が月8件未対応"}}\n'
        'または {"proposal": null}'
    )


def parse_proposal(raw):
    """提案JSONを検証（純粋関数）。idは小文字英数のみ＝hr.validate_hire互換。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    p = data.get("proposal")
    if not isinstance(p, dict):
        return None
    new_id = str(p.get("id") or "").strip()
    name = str(p.get("name") or "").strip()
    role = str(p.get("role") or "").strip()
    ch = str(p.get("channel_name") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", new_id):
        return None
    if not name or not role or not ch:
        return None
    return {"new_id": new_id, "name": name[:32], "role": role[:200],
            "channel_name": ch[:32], "temporary": bool(p.get("temporary")),
            "reason": str(p.get("reason") or "")[:120]}


def detect(db_path, roster, *, model, invoke_fn=None, now=None):
    """役割の空白を検出。提案dict or None。"""
    now = now or reminders.now_jst()
    signals = collect_signals(db_path, now)
    events = upcoming_events(db_path, now)
    if len(signals) < MIN_SIGNALS and not events:
        return None
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=DETECT_TIMEOUT_SEC).text)
    return parse_proposal(fn(build_prompt(signals, roster, events)))
