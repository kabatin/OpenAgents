#!/usr/bin/env python3
"""イベント逆算スケジュール（進化ロードマップ#35）。

決定台帳（RM#4）に「〜開催」系＋日付つきの決定が入ったら検知し、開催日から
逆算した準備の締切案（発注・入稿・告知開始・最終確認など）を提案する。
管理者の✅で各節目が action_items（納期追跡）に登録され、以後の声かけ
（2日前・当日・超過）は既存機構が全自動で行う。❌で見送り。

三段構成:
  1) 検知   : 決定台帳の新しい決定から開催系キーワード＋未来日付を正規表現で抽出
              （決定論。events.source_decision_id UNIQUE で二重提案しない）
  2) 逆算   : LLMが関連する過去ログ（FTS）を参考に節目を提案（日付は今日〜開催日
              の範囲でコードが検証。範囲外・壊れたJSONは捨てる＝安全側）
  3) 承認   : 提案投稿はそれ自体が承認依頼（✅までは何も追跡されない）。
              ✅で登録・❌で見送り（CAS排他・管理者のみ）

単体テスト: ./venv/bin/python -m unittest test_event_planner -v
"""

import json
import os
import re
from datetime import date, timedelta

from core import invoke_claude
from core import db
from core import reminders
from core import search
PLAN_TIMEOUT_SEC = 300
MAX_MILESTONES = 8
MAX_TASK_LEN = 60
# 開催系キーワード（決定文にこれと日付が両方あればイベント候補）
EVENT_KEYWORD_RE = re.compile(r"開催|実施|イベント|大会|発表会|展示")
# 日付表現: 8/29・8月29日・2026-08-29・2026/8/29
DATE_RE = re.compile(
    r"(?:(20\d{2})[-/年])?(\d{1,2})[-/月](\d{1,2})日?")
MENTION_RE = re.compile(r"<@!?\d+>")
_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_event_date(text, today):
    """文中から未来の開催日を1つ取り出して YYYY-MM-DD で返す（無ければ None）。
    年の無い「8/29」は今日以降になる年に解釈する。"""
    for m in DATE_RE.finditer(text or ""):
        year = int(m.group(1)) if m.group(1) else today.year
        try:
            d = date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if not m.group(1) and d < today:
            d = date(today.year + 1, d.month, d.day)
        if today <= d <= today + timedelta(days=400):
            return d.strftime("%Y-%m-%d")
    return None


def detect_events(decisions_rows, today):
    """決定行からイベント候補を抽出（純粋関数・テスト対象）。
    Returns: [{"decision_id","name","event_date","channel_id",
               "source_message_id"}]"""
    out = []
    for r in decisions_rows:
        text = r.get("decision") or ""
        if not EVENT_KEYWORD_RE.search(text):
            continue
        d = parse_event_date(text, today)
        if d is None:
            continue
        out.append({"decision_id": r["id"], "name": text[:60],
                    "event_date": d, "channel_id": r.get("channel_id"),
                    "source_message_id": r.get("source_message_id")})
    return out


def build_plan_prompt(event, today, context_block):
    """逆算案の生成プロンプト（純粋関数・テスト対象）。"""
    return (
        "社内イベントの準備スケジュールを開催日から逆算して提案して。\n\n"
        f"【イベント】{event['name']}\n"
        f"【開催日】{event['event_date']}（今日は {today}）\n\n"
        f"【関連する社内の過去ログ（参考）】\n{context_block or '（なし）'}\n\n"
        "ルール:\n"
        "- 節目は3〜6個。グッズ発注（リードタイム約3週間）・素材/入稿・"
        "告知開始（1〜2週間前）・最終確認（2日前）などイベント内容に合わせる\n"
        f"- due は YYYY-MM-DD で、今日以降〜開催日以前の現実的な日付にする\n"
        "- owners は過去ログから担当が明確な場合のみ <@数字> 形式で"
        "（不明なら空配列）\n"
        "- 出力はJSONのみ:\n"
        '{"milestones": [{"task": "グッズ発注の締切", "due": "2026-08-08", '
        '"owners": ["<@123>"]}]}'
    )


def parse_plan(raw, today, event_date):
    """逆算案JSONの検証つき解釈（純粋関数）。範囲外の日付・不正は捨てる。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in (data.get("milestones") or [])[:MAX_MILESTONES]:
        if not isinstance(it, dict):
            continue
        task = str(it.get("task") or "").strip()[:MAX_TASK_LEN]
        due = str(it.get("due") or "")
        if not task or not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
            continue
        if not (today <= due <= event_date):
            continue   # 過去日・開催日超えの節目は捨てる（LLMの計算ミス対策）
        owners = [o for o in (it.get("owners") or [])
                  if isinstance(o, str) and MENTION_RE.fullmatch(o.strip())]
        out.append({"task": task, "due_date": due,
                    "owners": " ".join(owners) or "（担当未定）",
                    "urgent": False})
    return sorted(out, key=lambda x: x["due_date"])


def plan(event, db_path, *, model=search.DEFAULT_MODEL, invoke_fn=None,
         today=None):
    """逆算案を生成（FTSで関連過去ログを裏取りに渡す）。"""
    today = today or reminders.now_jst().strftime("%Y-%m-%d")
    rows = search.search_messages(db_path, [event["name"][:20]], limit=8)
    context_block = search.build_context(rows, "0") if rows else ""
    prompt = build_plan_prompt(event, today, context_block)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=PLAN_TIMEOUT_SEC).text)
    return parse_plan(fn(prompt), today, event["event_date"])


def build_proposal(event, milestones):
    """提案メッセージ本文（純粋関数）。"""
    lines = [f"🗓️ 「{event['name']}」（{event['event_date']}開催）の"
             "逆算スケジュール案っス:"]
    for i, ms in enumerate(milestones, 1):
        lines.append(f"{i}. {ms['task']} → **{ms['due_date']}** "
                     f"（{ms['owners']}）")
    lines.append("-# 管理者の✅でこのまま納期追跡（2日前・当日の声かけ）に"
                 "載せるっス／❌で見送りっス")
    return "\n".join(lines)


def register_event(db_path, agent_id, event, milestones, status):
    """イベント＋逆算案を保存（同じ決定からは1回だけ）。event_id or None。"""
    with db.connect(db_path) as conn:
        return db.add_event(
            conn, agent_id=agent_id, name=event["name"],
            event_date=event["event_date"],
            source_decision_id=event["decision_id"],
            channel_id=event.get("channel_id"),
            milestones_json=json.dumps(milestones, ensure_ascii=False),
            status=status,
            created_at=reminders.fmt(reminders.now_jst()))


def approve(db_path, agent_id, message_id):
    """✅承認: 節目を action_items へ登録（CAS排他）。
    Returns: 登録件数（対象外/競合負けは None）。"""
    with db.connect(db_path) as conn:
        ev = db.event_by_proposal(conn, message_id)
        if ev is None or not db.claim_event(conn, ev["id"], "planned"):
            return None
        milestones = json.loads(ev["milestones_json"] or "[]")
        now = reminders.fmt(reminders.now_jst())
        ids = []
        for ms in milestones:
            ids.append(db.add_action_item(
                conn, agent_id=agent_id,
                source_message_id=message_id,
                channel_id=ev["channel_id"], task=ms["task"],
                owners=ms["owners"], due_date=ms["due_date"],
                urgent=False, created_at=now))
        db.set_action_confirm_message(conn, ids, message_id)
    return len(ids)


DESIGN_KEYWORD_RE = re.compile(r"デザイン|バナー|素材|入稿|ロゴ|グッズ|画像|ビジュアル")


def design_milestones(event_row):
    """イベントの節目からデザイン系だけを表示行にする（RM#40・純粋関数）。"""
    try:
        milestones = json.loads(event_row.get("milestones_json") or "[]")
    except ValueError:
        return []
    return [f"{ms['task']} → {ms['due_date']}" for ms in milestones
            if DESIGN_KEYWORD_RE.search(ms.get("task") or "")]


def dismiss(db_path, message_id):
    """❌見送り（CAS排他）。対象だったら True。"""
    with db.connect(db_path) as conn:
        ev = db.event_by_proposal(conn, message_id)
        if ev is None:
            return False
        return db.claim_event(conn, ev["id"], "dismissed")
