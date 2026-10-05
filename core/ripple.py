#!/usr/bin/env python3
"""決定の波及チェッカー。

新しい決定が台帳に入ったとき、それと衝突・連動する既存の記録
（旧決定・openタスク・アクティブなリマインダー・予定イベント）を横断チェックし、
**直す必要があるものだけ**を具体的な案にして1本の提案を出す。

「タスク id=19 に影響」のような番号だけの投稿は、何が困って何を判断すればいいか
分からず放置されていた（一致・具体化しただけのものまで出ていた）ので:
- 影響の種類（effect）を区別: conflict=旧決定と矛盾 / reschedule=日付がずれる /
  related=関係はあるが直すものはない / same=一致・具体化しただけ
- 投稿は conflict と reschedule だけ。related・same は黙って記録する
- 影響先は中身つき（タスクなら「A18『〜』（担当・期日）」）で、案は1つ
- ✅でその案を反映: 矛盾する旧決定を上書き済みに、タスクの期日を新しい日付に、
  ずれるリマインダーは直し方の案（reminder_shift.py が日付を計算）のとおりに。
  予定イベントと、案を作れないリマインダーは「自動では直せない」と明示し、
  投稿への返信で直し方を教えてもらう（教わった直し方は失敗の台帳に見本として残る）

トリガーは「新しい決定が入ったとき」だけ＝発火頻度は極めて低い。

単体テスト: platforms/discord/test_treasure_pack.py
"""

import json
import re

from core import db
from core import invoke_claude
from core import reminder_shift
from core import reminders

STATE_KEY = "ripple:"
MAX_PER_CYCLE = 3
TIMEOUT_SEC = 180
KINDS = {"decision": "旧決定", "action_item": "タスク",
         "reminder": "リマインダー", "event": "イベント"}
_JSON_RE = re.compile(r"\{.*\}", re.S)
_TERM_RE = re.compile(r"[ァ-ヶー一-龠a-zA-Z0-9]{2,}")
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACTIONABLE = ("conflict", "reschedule")
_REPEAT = {"once": "1回", "daily": "毎日", "weekly": "毎週",
           "monthly": "毎月", "monthly_end": "毎月末"}
_URL_RE = re.compile(r"https?://\S+")


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


def _owner_names(conn, owners):
    """担当者欄の <@ID> を表示名に（引けなければ空）。"""
    names = []
    for uid in _MENTION_RE.findall(owners or ""):
        row = conn.execute("SELECT display_name FROM users WHERE id=?",
                           (int(uid),)).fetchone()
        if row and row[0]:
            names.append(row[0])
    return "・".join(names)


def gather_candidates(db_path, new_decision):
    """突合対象の既存記録（決定論）。新決定と語が重なる旧決定＋全体の
    openタスク・アクティブリマインダー・予定イベント。"""
    terms = _TERM_RE.findall(new_decision["decision"] or "")[:4]
    with db.connect(db_path) as conn:
        old_decisions = [d for d in db.search_decisions(conn, terms, limit=8)
                         if d["id"] != new_decision["id"]] if terms else []
        items = [dict(it, owner=_owner_names(conn, it.get("owners")))
                 for it in db.all_open_action_items(conn)]
        events = db.planned_events(conn)
    # 直し方の案（reminder_shift）を作るため、リマインダーは丸ごと持つ
    rems = [dict(r) for r in reminders.list_active()]
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
            lambda r: f"  id={r['id']} {r['content'][:60]}"
                      f"（次回{r['due']}・{_REPEAT.get(r.get('repeat'), r.get('repeat'))}）")
        + "\n◆予定イベント:\n" + _fmt(
            cands["events"],
            lambda r: f"  id={r['id']} {r['name'][:60]}（{r['event_date']}）")
        + "\n\n影響ごとに effect を1つ選ぶ:\n"
        "- conflict: 旧決定と**明確に矛盾**する（同じ事柄の別の結論）。旧決定だけに使う\n"
        "- reschedule: 新決定で**日付がずれる**タスク・リマインダー・イベント。"
        "新しい日付が決定から読み取れるなら new_due に YYYY-MM-DD で入れる\n"
        "- related: 関係はあるが、直すものはない\n"
        "- same: 新決定が既存の記録と一致・具体化しているだけ\n"
        "リマインダーを reschedule にするときは shift も入れる: weekday=新しい曜日"
        "（0=月〜6=日）、start=新しい曜日になる最初の日、end=元に戻る前の最後の日"
        "（以降ずっとなら null）、content=期間中に流す文面（元の文面の曜日だけ直す）、"
        "temporary=決定が一時的な変更か（true/false。終わりの日が書かれていなくても"
        "一時的なら true）\n"
        "関係が薄いものは含めない（誤検知は信頼を削る）。why は人が読んで分かる短い日本語で。\n"
        '出力はJSONのみ。影響なしなら {"impacts": []}:\n'
        '{"impacts": [{"kind": "decision|action_item|reminder|event", "id": 3, '
        '"effect": "conflict|reschedule|related|same", "new_due": "2026-09-05", '
        '"shift": {"weekday": 3, "start": "2026-10-15", "end": "2026-11-12", '
        '"content": "今週は木曜日が定例です", "temporary": true}, '
        '"why": "開催日が9/5に延期されたため"}]}'
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
    details = {
        "decision": {r["id"]: {"label": r["decision"][:40]}
                     for r in cands["decisions"]},
        "action_item": {r["id"]: {"label": r["task"][:40],
                                  "due": r["due_date"],
                                  "owner": r.get("owner", "")}
                        for r in cands["action_items"]},
        "reminder": {r["id"]: {"label": _URL_RE.sub("", r["content"]).strip()[:40],
                               "due": r["due"]}
                     for r in cands["reminders"]},
        "event": {r["id"]: {"label": r["name"][:40], "due": r["event_date"]}
                  for r in cands["events"]}}
    valid = {k: set(v) for k, v in details.items()}
    entries = {r["id"]: r for r in cands["reminders"]}
    out = []
    for it in (data.get("impacts") or [])[:8]:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "")
        try:
            iid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        if kind not in valid or iid not in valid[kind]:
            continue
        effect = str(it.get("effect") or "related")
        effect = effect if effect in ("conflict", "reschedule", "related",
                                      "same") else "related"
        if effect == "conflict" and kind != "decision":
            effect = "related"
        new_due = str(it.get("new_due") or "")
        if effect == "reschedule" and kind == "action_item" \
                and not _DATE_RE.match(new_due):
            effect = "related"      # 日付が読めないものは自動では変えない
        row = details[kind].get(iid, {})
        plan = None
        if kind == "reminder" and effect == "reschedule":
            plan = _shift_plan(it.get("shift"), entries.get(iid))
        out.append({"kind": kind, "id": iid, "effect": effect, "plan": plan,
                    "new_due": new_due if _DATE_RE.match(new_due) else None,
                    "old_due": row.get("due"), "label": row.get("label", ""),
                    "owner": row.get("owner", ""),
                    "why": str(it.get("why") or "")[:100]})
    return out


def _shift_plan(shift, entry):
    """AIが読み取った「曜日・期間・文面」から、日付を計算した直し方の案を作る。
    読み取れない・対応外（毎日・毎月など）なら None（人が直し方を教える）。"""
    if not isinstance(shift, dict) or entry is None:
        return None
    try:
        weekday = int(shift.get("weekday"))
    except (TypeError, ValueError):
        return None
    content = shift.get("content")
    p = reminder_shift.plan(
        entry, now=reminders.now_jst(), weekday=weekday,
        start=shift.get("start"), end=shift.get("end") or None,
        content=str(content)[:300] if content else None)
    if p is not None and shift.get("temporary") is True and not shift.get("end"):
        # 一時的なのに終わりの日が決定に無い＝戻す日は人しか知らない
        p["ask_end"] = True
    return p


def actionable(impacts):
    """投稿に値する影響（矛盾・日付ずれ）だけ。一致・関連だけなら黙る。"""
    return [it for it in impacts if it.get("effect") in ACTIONABLE]


def check(db_path, new_decision, *, model, invoke_fn=None):
    cands = gather_candidates(db_path, new_decision)
    if not any(cands.values()):
        return []
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC, purpose="ripple").text)
    return parse_impacts(fn(build_prompt(new_decision, cands)), cands)


def _md(due):
    """'2026-10-16' / '2026-10-16T09:00' → '10/16'（読めなければそのまま）。"""
    m = re.match(r"\d{4}-(\d{2})-(\d{2})", str(due or ""))
    return f"{int(m.group(1))}/{int(m.group(2))}" if m else (due or "期日なし")


def _line(n, it):
    why = f" — {it['why']}" if it.get("why") else ""
    if it["kind"] == "decision":
        return (f"{n}. 🗂 決定#{it['id']}「{it.get('label', '')}」と食い違い"
                f" → 新しい決定を正として、古い方を上書き済みにする{why}")
    if it["kind"] == "action_item":
        owner = f"（{it['owner']}）" if it.get("owner") else ""
        if it.get("new_due"):
            return (f"{n}. 📌 タスクA{it['id']}「{it.get('label', '')}」{owner}"
                    f" 期日 {_md(it.get('old_due'))} → **{_md(it['new_due'])}**{why}")
        return (f"{n}. 📌 タスクA{it['id']}「{it.get('label', '')}」{owner}"
                f" 期日 {_md(it.get('old_due'))} がずれそうです"
                f"（新しい日付を教えてください）{why}")
    label = "⏰ リマインダー" if it["kind"] == "reminder" else "🗓 予定"
    if it.get("plan"):
        rows = reminder_shift.preview(it["plan"])
        if it["plan"].get("ask_end"):
            rows.append("一時的とのことなので、戻す日が決まったら教えてください"
                        "（そこで元の曜日に戻します）")
        steps = "\n".join(f"   ・{x}" for x in rows)
        return (f"{n}. {label}#{it['id']}「{it.get('label', '')}」を次のように直す案{why}\n"
                f"{steps}")
    return (f"{n}. {label}#{it['id']}「{it.get('label', '')}」"
            f"（{_md(it.get('old_due'))}）→ 日付を直す必要あり（自動では直せない）{why}")


def describe(it):
    """1件の影響を番号なしの1行で（失敗の台帳に残す説明）。"""
    return _line(0, it).split(". ", 1)[-1]


def auto_applicable(it):
    return bool((it["kind"] == "decision" and it.get("effect") == "conflict") or (
        it["kind"] == "action_item" and it.get("effect") == "reschedule"
        and it.get("new_due")) or (it["kind"] == "reminder" and it.get("plan")))


def build_proposal(new_decision, impacts):
    """直す案の投稿（actionable な影響だけ渡す）。✅が何をするかを必ず書く。"""
    lines = [f"🌊 新しい決定「{new_decision['decision'][:60]}」に合わせて、"
             "直したほうがよさそうな記録があります:"]
    auto, manual = [], []
    for n, it in enumerate(impacts, 1):
        lines.append(_line(n, it))
        (auto if auto_applicable(it) else manual).append(str(n))
    tail = (f"✅＝{'・'.join(auto)} をそのまま反映する" if auto else "✅＝確認した")
    tail += "／❌＝何もしない（誤検知）"
    if manual:
        # 直し方をエージェントの手元で教わる＝正解の見本が台帳に残る（改善の材料）
        tail += (f"／{'・'.join(manual)} は、この投稿に返信で直し方を教えて"
                 "もらえれば対応します")
    lines.append(f"-# {tail}")
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
    """✅: 案を反映する（CAS排他）。矛盾する旧決定は superseded、日付がずれる
    タスクは新しい期日へ（声かけはやり直し）、リマインダーは直し方の案のとおりに。
    予定イベントは触らない。
    Returns: {"decisions": n, "dues": m, "reminders": k}（対象外・決定済みは None）。"""
    with db.connect(db_path) as conn:
        prop = db.ripple_by_message(conn, message_id)
        if prop is None or not db.claim_ripple(conn, prop["id"], "applied"):
            return None
        done = {"decisions": 0, "dues": 0, "reminders": 0}
        for it in json.loads(prop["impacts_json"] or "[]"):
            if it["kind"] == "decision" and it.get("effect", "conflict") == "conflict":
                if db.supersede_decision(conn, it["id"]):
                    done["decisions"] += 1
            elif (it["kind"] == "action_item" and it.get("effect") == "reschedule"
                  and it.get("new_due")):
                row = conn.execute("SELECT agent_id FROM action_items WHERE id=?",
                                   (it["id"],)).fetchone()
                if row and db.reschedule_action_item(conn, it["id"], row[0],
                                                     it["new_due"]) is not None:
                    done["dues"] += 1
            elif it["kind"] == "reminder" and it.get("plan"):
                res = reminder_shift.apply(it["plan"])
                if res["added"] or res["moved"]:
                    done["reminders"] += 1
        return done


def applied_note(done):
    """✅で反映した結果の報告（やったことだけを書く）。"""
    parts = []
    if done.get("dues"):
        parts.append(f"タスク{done['dues']}件の期日を直し")
    if done.get("reminders"):
        parts.append(f"リマインダー{done['reminders']}件を案のとおり付け替え")
    if done.get("decisions"):
        parts.append(f"古い決定{done['decisions']}件を上書き済みにし")
    if not parts:
        return "確認済みにしました（自動で直したものはありません）"
    return "、".join(parts) + "ました"


def decision_for(db_path, message_id):
    """提案の投稿から決定の本文を引く（提案でなければ None）。
    返信で直し方を教わったときに「どの決定の話か」を見本に残すため。"""
    with db.connect(db_path) as conn:
        row = conn.execute(
            """SELECT d.decision FROM ripple_proposals p
               JOIN decisions d ON d.id = p.decision_id
               WHERE p.proposal_message_id=?""", (message_id,)).fetchone()
    return row[0] if row else None


def gap_context(decision):
    """失敗の台帳の「場面」の書き方（記録時と、教わったときで同じにする）。"""
    return f"決定「{(decision or '')[:60]}」"


def context_for(db_path, message_id):
    """提案の投稿から「どの決定の波及案か」を引く（❌の理由を聞くときの見出し）。"""
    decision = decision_for(db_path, message_id)
    return f"決定「{decision[:60]}」の波及案" if decision else "決定の波及案"


def dismiss(db_path, message_id):
    with db.connect(db_path) as conn:
        prop = db.ripple_by_message(conn, message_id)
        if prop is None:
            return False
        return db.claim_ripple(conn, prop["id"], "dismissed")
