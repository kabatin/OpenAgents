#!/usr/bin/env python3
"""決定の波及チェッカー（とっておき#101）。

新しい決定が台帳に入ったとき、それと衝突・連動する既存の記録
（旧決定・openタスク・アクティブなリマインダー・予定イベント）を横断チェックし、
影響がある場合だけ1本の提案を出す。管理者の✅で矛盾する旧決定をsupersededに
する（タスク・リマインダー・イベントは一覧提示のみ＝日付の付け替えは人間が
内容を決める。勝手に書き換えない）。

トリガーは「新しい決定が入ったとき」だけ＝発火頻度は極めて低い。

単体テスト: ./venv/bin/python -m unittest test_treasure_pack -v
"""

import json
import os
import re

from core import invoke_claude
from core import db
from core import reminders
STATE_KEY = "ripple:"
MAX_PER_CYCLE = 3
TIMEOUT_SEC = 180
KINDS = {"decision": "旧決定", "action_item": "タスク",
         "reminder": "リマインダー", "event": "イベント"}
_JSON_RE = re.compile(r"\{.*\}", re.S)
_TERM_RE = re.compile(r"[ァ-ヶー一-龠a-zA-Z0-9]{2,}")


def checkpoint(db_path, agent_id):
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_KEY + agent_id)
    return (state or {}).get("last_checked_message_id") or 0


def save_checkpoint(db_path, agent_id, decision_id):
    with db.connect(db_path) as conn:
        db.set_proactive_state(
            conn, STATE_KEY + agent_id,
            last_checked_message_id=decision_id,
            last_run_at=reminders.fmt(reminders.now_jst()))


def gather_candidates(db_path, new_decision):
    """突合対象の既存記録（決定論）。新決定と語が重なる旧決定＋全体の
    openタスク・アクティブリマインダー・予定イベント。"""
    terms = _TERM_RE.findall(new_decision["decision"] or "")[:4]
    with db.connect(db_path) as conn:
        old_decisions = [d for d in db.search_decisions(conn, terms, limit=8)
                         if d["id"] != new_decision["id"]] if terms else []
        items = db.all_open_action_items(conn)
        events = db.planned_events(conn)
    rems = [{"id": r["id"], "content": r["content"], "due": r["due"]}
            for r in reminders.list_active()]
    return {"decisions": old_decisions, "action_items": items,
            "reminders": rems, "events": events}


def build_prompt(new_decision, cands):
    def _fmt(rows, fmt):
        return "\n".join(fmt(r) for r in rows) or "（なし）"
    return (
        "新しい決定が入った。影響を受ける既存の記録があるか判定して。\n\n"
        f"【新しい決定】{new_decision['decision']}\n\n"
        "【既存の記録】\n"
        "◆旧決定:\n" + _fmt(cands["decisions"],
                            lambda r: f"  id={r['id']} {r['decision'][:80]}")
        + "\n◆進行中タスク:\n" + _fmt(
            cands["action_items"],
            lambda r: f"  id={r['id']} {r['task'][:60]}（期日{r['due_date']}）")
        + "\n◆リマインダー:\n" + _fmt(
            cands["reminders"],
            lambda r: f"  id={r['id']} {r['content'][:60]}（{r['due']}）")
        + "\n◆予定イベント:\n" + _fmt(
            cands["events"],
            lambda r: f"  id={r['id']} {r['name'][:60]}（{r['event_date']}）")
        + "\n\n判定基準:\n"
        "- 新決定と**明確に矛盾**する旧決定（同じ事柄の別の結論）\n"
        "- 新決定で**前提が変わる**タスク・リマインダー・イベント"
        "（日程変更で期日がズレる等）\n"
        "- 関係が薄いもの・自明に共存できるものは含めない（誤検知は信頼を削る）\n"
        '出力はJSONのみ。影響なしなら {"impacts": []}:\n'
        '{"impacts": [{"kind": "decision|action_item|reminder|event", '
        '"id": 3, "why": "開催日が矛盾（旧8/29 vs 新9/5）"}]}'
    )


def parse_impacts(raw, cands):
    """影響リストの検証つき解釈。提示していないidは捨てる（純粋関数）。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    valid = {"decision": {r["id"] for r in cands["decisions"]},
             "action_item": {r["id"] for r in cands["action_items"]},
             "reminder": {r["id"] for r in cands["reminders"]},
             "event": {r["id"] for r in cands["events"]}}
    out = []
    for it in (data.get("impacts") or [])[:8]:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "")
        try:
            iid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        if kind in valid and iid in valid[kind]:
            out.append({"kind": kind, "id": iid,
                        "why": str(it.get("why") or "")[:100]})
    return out


def check(db_path, new_decision, *, model, invoke_fn=None):
    cands = gather_candidates(db_path, new_decision)
    if not any(cands.values()):
        return []
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC).text)
    return parse_impacts(fn(build_prompt(new_decision, cands)), cands)


def build_proposal(new_decision, impacts):
    lines = [f"🌊 新しい決定「{new_decision['decision'][:60]}」で"
             "影響が出そうな記録があるっス:"]
    for it in impacts:
        lines.append(f"- [{KINDS[it['kind']]} id={it['id']}] {it['why']}")
    has_dec = any(it["kind"] == "decision" for it in impacts)
    tail = ("✅で矛盾する旧決定を上書き済みにするっス"
            if has_dec else "✅で確認済みにするっス")
    lines.append(f"-# 管理者の{tail}／タスク・リマインダーの付け替えは"
                 "内容を教えてもらえれば直すっス／❌で誤検知として見送りっス")
    return "\n".join(lines)


def register(db_path, decision_id, impacts):
    with db.connect(db_path) as conn:
        return db.add_ripple_proposal(
            conn, decision_id=decision_id,
            impacts_json=json.dumps(impacts, ensure_ascii=False),
            created_at=reminders.fmt(reminders.now_jst()))


def set_message(db_path, proposal_id, message_id):
    with db.connect(db_path) as conn:
        db.set_ripple_message(conn, proposal_id, message_id)


def approve(db_path, message_id):
    """✅: 矛盾として挙がった旧決定をsupersededへ（CAS排他）。
    Returns: superseded件数（対象外は None）。"""
    with db.connect(db_path) as conn:
        prop = db.ripple_by_message(conn, message_id)
        if prop is None or not db.claim_ripple(conn, prop["id"], "applied"):
            return None
        n = 0
        for it in json.loads(prop["impacts_json"] or "[]"):
            if it["kind"] == "decision" and db.supersede_decision(
                    conn, it["id"]):
                n += 1
        return n


def dismiss(db_path, message_id):
    with db.connect(db_path) as conn:
        prop = db.ripple_by_message(conn, message_id)
        if prop is None:
            return False
        return db.claim_ripple(conn, prop["id"], "dismissed")
