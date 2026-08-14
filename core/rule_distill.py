#!/usr/bin/env python3
"""週次ルール蒸留（進化ロードマップ#2）。

rulesテーブルは使うほど重複・陳腐化が混ざる。週1回、全エージェントの
activeルールをLLMが棚卸しし、「無効化すべきもの」を提案として相談室へ投稿。
管理者の✅で無効化を実行（❌で見送り）。判断は人間・提案はLLM・実行はコード。

単体テスト: ./venv/bin/python -m unittest test_rule_distill -v
"""

import json
import os
import re

from core import invoke_claude
from core import db
from core import reminders
STATE_KEY_PREFIX = "ruledistill:"
WEEKDAY_DEFAULT = 0    # 月曜
HOUR_DEFAULT = 10
MIN_RULES = 8          # これ未満なら棚卸し不要（ノイズ防止）
MAX_PROPOSALS = 5
DISTILL_TIMEOUT_SEC = 180
_JSON_RE = re.compile(r"\{.*\}", re.S)


def should_send(db_path, agent_id, *, weekday=WEEKDAY_DEFAULT,
                hour=HOUR_DEFAULT, now=None):
    now = now or reminders.now_jst()
    if now.weekday() != weekday or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_KEY_PREFIX + agent_id)
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
        db.set_proactive_state(conn, STATE_KEY_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def build_prompt(rules_rows, today):
    lines = [f"  id={r['id']} [{r['agent_id']}/{r['scope']}] {r['rule_text']}"
             + (f"（期限:{r['expires_at']}）" if r.get("expires_at") else "")
             for r in rules_rows]
    return (
        "社内AIエージェントたちの恒久ルール一覧を棚卸しして。\n"
        f"（今日は {today}）\n\n【ルール一覧】\n" + "\n".join(lines) + "\n\n"
        "無効化を提案すべきもの:\n"
        "- 明確な重複（同じ内容のルールが複数）→ 古い方を無効化\n"
        "- 明確に陳腐化（過ぎたイベント・終わった話への言及）\n"
        "- 相互に矛盾するルール → どちらを残すか提案\n"
        f"確実なものだけ最大{MAX_PROPOSALS}件。迷ったら提案しない"
        "（消しすぎは学習の喪失）。\n"
        "出力はJSONのみ: {\"proposals\": [{\"rule_id\": 3, "
        "\"reason\": \"id=5と重複\"}]}\n"
        "提案なしなら {\"proposals\": []}"
    )


def parse_proposals(raw, rules_by_id):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for p in (data.get("proposals") or [])[:MAX_PROPOSALS]:
        if not isinstance(p, dict):
            continue
        try:
            rid = int(p.get("rule_id"))
        except (TypeError, ValueError):
            continue
        if rid not in rules_by_id:
            continue
        out.append({"rule_id": rid,
                    "agent_id": rules_by_id[rid]["agent_id"],
                    "rule_text": rules_by_id[rid]["rule_text"],
                    "reason": str(p.get("reason") or "")[:100]})
    return out


def distill(db_path, *, model, invoke_fn=None, now=None):
    """棚卸し実行。提案リスト（無ければ空）。ルールが少なければ空。"""
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        rows = db.rules_all_active(conn)
    if len(rows) < MIN_RULES:
        return []
    by_id = {r["id"]: r for r in rows}
    prompt = build_prompt(rows, now.strftime("%Y-%m-%d"))
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=DISTILL_TIMEOUT_SEC).text)
    return parse_proposals(fn(prompt), by_id)


def build_proposal_text(proposals):
    lines = ["🧹 ルールの棚卸し提案っス（増えてきたので整理したいっス）:"]
    for i, p in enumerate(proposals, 1):
        lines.append(f"{i}. id={p['rule_id']}「{p['rule_text'][:50]}」"
                     f"→ 無効化（{p['reason']}）")
    lines.append("-# 管理者の✅でまとめて無効化するっス／❌で今回は見送りっス")
    return "\n".join(lines)


def register(db_path, proposals):
    with db.connect(db_path) as conn:
        return db.add_rule_review(
            conn, payload_json=json.dumps(proposals, ensure_ascii=False),
            created_at=reminders.fmt(reminders.now_jst()))


def set_message(db_path, review_id, message_id):
    with db.connect(db_path) as conn:
        db.set_rule_review_message(conn, review_id, message_id)


def approve(db_path, message_id):
    """✅で提案のルールを無効化（CAS排他）。無効化件数 or None。"""
    with db.connect(db_path) as conn:
        review = db.rule_review_by_message(conn, message_id)
        if review is None or not db.claim_rule_review(conn, review["id"],
                                                      "applied"):
            return None
        n = 0
        for p in json.loads(review["payload_json"] or "[]"):
            if db.deactivate_rule(conn, p["rule_id"], p["agent_id"]):
                n += 1
        return n


def dismiss(db_path, message_id):
    with db.connect(db_path) as conn:
        review = db.rule_review_by_message(conn, message_id)
        if review is None:
            return False
        return db.claim_rule_review(conn, review["id"], "dismissed")
