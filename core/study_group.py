#!/usr/bin/env python3
"""エージェント間勉強会（進化ロードマップ#61）。

個体が学んだルール（scope=channel/user）のうち「全員が知っておくべき」
ものを月次で選び、globalへ昇格する提案を出す。✅で昇格・❌で見送り
（判断は人間＝ルールの物差しは人間が持つ、の原則）。

単体テスト: ./venv/bin/python -m unittest test_meta_pack -v
"""

import json
import os
import re

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "study:"
DAY_DEFAULT = 2
HOUR_DEFAULT = 15
MIN_RULES = 4
MAX_PICKS = 2
TIMEOUT_SEC = 120
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


def build_prompt(rules_rows):
    listing = "\n".join(
        f"  id={r['id']} [{r['agent_id']}/{r['scope']}] {r['rule_text']}"
        for r in rules_rows)
    return (
        "AIエージェントが個別に学んだルールの中から、"
        "**全員が知っておくべきもの**を選んで。\n\n"
        f"【個別ルール】\n{listing}\n\n"
        "選ぶ基準:\n"
        "- 特定の人・特定chだけの好みではなく、社内共通の事実や作法\n"
        "- 他のエージェントが知らないと同じ失敗を繰り返すもの\n"
        f"- 最大{MAX_PICKS}件。該当なしなら空（広げすぎない）\n"
        '出力はJSONのみ: {"picks": [{"rule_id": 3, '
        '"reason": "全員が同じ表記を使うべき"}]}'
    )


def parse_picks(raw, by_id):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for p in (data.get("picks") or [])[:MAX_PICKS]:
        if not isinstance(p, dict):
            continue
        try:
            rid = int(p.get("rule_id"))
        except (TypeError, ValueError):
            continue
        if rid in by_id:
            out.append({**by_id[rid], "reason": str(p.get("reason") or "")[:100]})
    return out


def find_shareable(db_path, *, model, invoke_fn=None):
    with db.connect(db_path) as conn:
        rows = db.non_global_rules(conn)
    if len(rows) < MIN_RULES:
        return []
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC).text)
    return parse_picks(fn(build_prompt(rows)), {r["id"]: r for r in rows})


def build_post(picks):
    lines = ["📚 勉強会っス！この学びは全員で共有した方が良さそうっス:"]
    for p in picks:
        lines.append(f"- id={p['rule_id'] if 'rule_id' in p else p['id']}"
                     f"「{p['rule_text'][:60]}」（{p['reason']}）")
    lines.append("-# ✅で全体ルール（global）に昇格するっス／❌で見送りっス")
    return "\n".join(lines)


def register(db_path, picks, from_agent):
    ids = []
    now = reminders.fmt(reminders.now_jst())
    with db.connect(db_path) as conn:
        for p in picks:
            ids.append(db.add_share_proposal(
                conn, rule_id=p["id"], from_agent=from_agent,
                created_at=now))
    return ids


def set_message(db_path, proposal_ids, message_id):
    with db.connect(db_path) as conn:
        for pid in proposal_ids:
            db.set_share_message(conn, pid, message_id)


def approve(db_path, message_id):
    """✅で昇格（CAS排他）。昇格件数 or None。"""
    n = 0
    with db.connect(db_path) as conn:
        while True:
            prop = db.share_proposal_by_message(conn, message_id)
            if prop is None:
                break
            if not db.claim_share_proposal(conn, prop["id"], "applied"):
                break
            if db.promote_rule_to_global(conn, prop["rule_id"]):
                n += 1
    return n or None


def dismiss(db_path, message_id):
    dismissed = False
    with db.connect(db_path) as conn:
        while True:
            prop = db.share_proposal_by_message(conn, message_id)
            if prop is None:
                break
            if not db.claim_share_proposal(conn, prop["id"], "dismissed"):
                break
            dismissed = True
    return dismissed
