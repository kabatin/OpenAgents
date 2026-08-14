#!/usr/bin/env python3
"""定型作業の自動化発見（進化ロードマップ#24）。

毎月、納期タスク・宿題・決定の蓄積から「毎回人間が手でやっている繰り返し作業」を
LLMが探し、自動化アイデアを相談室へ提案する。管理者の👍で capability_requests に
起票され、開発BOTの自動拾い上げ（RM#21）が「着手しますか？」と続きを引き取る
＝**発見→起票→実装が全部つながる**。

単体テスト: ./venv/bin/python -m unittest test_batch_pack -v
"""

import json
import os
import re
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "autodiscover:"
DAY_DEFAULT = 2
HOUR_DEFAULT = 11
MIN_ITEMS = 8          # 材料が少ないうちは走らない
MAX_IDEAS = 2
DISCOVER_TIMEOUT_SEC = 180
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


def collect_tasks(db_path, since):
    """自動化候補の材料（最近のタスク・宿題・決定のテキスト）。"""
    out = []
    with db.connect(db_path) as conn:
        for (t,) in conn.execute(
                "SELECT task FROM action_items WHERE created_at >= ?",
                (since,)).fetchall():
            out.append(t)
        for (t,) in conn.execute(
                "SELECT task FROM homework_items WHERE created_at >= ?",
                (since,)).fetchall():
            out.append(t)
        for (t,) in conn.execute(
                "SELECT decision FROM decisions WHERE created_at >= ?",
                (since,)).fetchall():
            out.append(t)
    return [t for t in out if (t or "").strip()]


def build_prompt(tasks):
    listing = "\n".join(f"- {t[:80]}" for t in tasks[:60])
    return (
        "社内チームのタスク・宿題・決定の記録から、「毎回人間が手作業で"
        "やっている繰り返し作業」を見つけて、自動化のアイデアを提案して。\n\n"
        f"【最近の記録】\n{listing}\n\n"
        "ルール:\n"
        f"- 確実に繰り返しが見えるものだけ最大{MAX_IDEAS}件（無ければ空）\n"
        "- 1回きりの作業・自動化済みのもの（リマインド・議事録・納期声かけ等）は除外\n"
        '- 出力はJSONのみ: {"ideas": [{"title": "短い名前", '
        '"desc": "何をどう自動化するか1〜2文"}]}'
    )


def parse_ideas(raw):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for i in (data.get("ideas") or [])[:MAX_IDEAS]:
        if not isinstance(i, dict):
            continue
        title = str(i.get("title") or "").strip()[:40]
        desc = str(i.get("desc") or "").strip()[:200]
        if title and desc:
            out.append({"title": title, "desc": desc})
    return out


def discover(db_path, *, model, invoke_fn=None, now=None):
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=45))
    tasks = collect_tasks(db_path, since)
    if len(tasks) < MIN_ITEMS:
        return []
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=DISCOVER_TIMEOUT_SEC).text)
    return parse_ideas(fn(build_prompt(tasks)))


def build_post(ideas):
    lines = ["🤖 定型作業の自動化アイデアっス（記録から見つけたっス）:"]
    for i, idea in enumerate(ideas, 1):
        lines.append(f"{i}. **{idea['title']}** — {idea['desc']}")
    lines.append("-# 管理者の👍で起票するっス（開発BOTちゃんが拾って"
                 "「着手しますか？」って聞いてくれるっスよ）／❌で見送りっス")
    return "\n".join(lines)


def register(db_path, ideas):
    with db.connect(db_path) as conn:
        return db.add_auto_proposal(
            conn, payload_json=json.dumps(ideas, ensure_ascii=False),
            created_at=reminders.fmt(reminders.now_jst()))


def set_message(db_path, proposal_id, message_id):
    with db.connect(db_path) as conn:
        db.set_auto_proposal_message(conn, proposal_id, message_id)


def approve(db_path, agent_id, message_id, requested_by):
    """👍で各アイデアを起票（CAS排他）。起票id一覧 or None。"""
    with db.connect(db_path) as conn:
        prop = db.auto_proposal_by_message(conn, message_id)
        if prop is None or not db.claim_auto_proposal(conn, prop["id"],
                                                      "approved"):
            return None
        now = reminders.fmt(reminders.now_jst())
        ids = []
        for idea in json.loads(prop["payload_json"] or "[]"):
            ids.append(db.add_capability_request(
                conn, agent_id=agent_id,
                description=f"{idea['title']} — {idea['desc']}",
                context="定型作業の自動化発見（RM#24）から管理者が👍承認",
                requested_by=str(requested_by), source_msg_id=message_id,
                created_at=now))
        return ids


def dismiss(db_path, message_id):
    with db.connect(db_path) as conn:
        prop = db.auto_proposal_by_message(conn, message_id)
        if prop is None:
            return False
        return db.claim_auto_proposal(conn, prop["id"], "dismissed")
