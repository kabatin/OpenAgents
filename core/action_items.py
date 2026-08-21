#!/usr/bin/env python3
"""
議事録の納期追跡（エージェントv3 Phase B）。設計: docs/agents-v3-proactive.md §4

議事録BOT（Webhook）が ai議事録 ch に投稿する定例議事録には
「☐ TODO: 内容（担当: <@id> ／期限: …）」形式の行が含まれる。これを:
  1) 抽出   : 観察ループが新着議事録を検知 → claude で担当・期日つきTODOを構造抽出
  2) 確認   : 追跡宣言を1回だけ投稿（誤抽出は管理者の❌で一括取り消し）
  3) 声かけ : 期日2日前・当日朝・超過翌朝に担当者へメンション（8〜22時のみ）
  4) 完了   : 声かけメッセージへの✅で done

原則:
  - 期日を具体日に解決できないTODO（「できるだけ早く」等）は追跡しない
    （勝手に期日を発明しない。件数だけ追跡宣言で正直に報告する）
  - 声かけは静的文面（claude を通さない: 定時通知の確実性優先＝reminders と同じ流儀）
  - 声かけは日次枠と別勘定（proactive_log の action='nudge'/'track' は枠に数えない）

Discordへの投稿・ループは bot.py（_minutes_cycle / _nudge_cycle）が持つ。
単体テスト: ./venv/bin/python -m unittest test_action_items -v
"""

import difflib
import json
import os
import re
from datetime import datetime, timedelta

from core import invoke_claude
from core import db
from core import reminders
from core import search
EXTRACT_TIMEOUT_SEC = 300
MAX_MINUTES_CHARS = 15000   # 抽出プロンプトに載せる議事録本文の上限
MAX_TASK_LEN = 200
MAX_ITEMS = 20              # 1議事録から追跡する上限（暴走防止）
NUDGE_PER_CYCLE = 5         # 1周期の声かけ上限（ch連投防止）
NUDGE_HOURS = range(8, 22)  # 声かけしてよい時間帯（JST）

# 観察ループのcheckpointと同居させるための専用キー接頭辞
STATE_PREFIX = "minutes:"

MENTION_RE = re.compile(r"<@!?\d+>")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_JSON_RE = re.compile(r"\{.*\}", re.S)

# 声かけ段階の順序（desired > current のときだけ声かけ＝重複防止）
STAGES = ("none", "before", "day", "overdue")


def _jst_date(created_at):
    """messages.created_at（ISO・+00:00想定）→ JSTの YYYY-MM-DD。"""
    try:
        dt = datetime.fromisoformat((created_at or "").replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None) + timedelta(hours=9)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return reminders.now_jst().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 1) 検知・抽出

def collect_new_minutes(db_path, agent_id, channel_id, now=None):
    """議事録chの新着（Bot/Webhook投稿のみ）をまとめて返し、checkpointを進める。
    初回は「今」に初期化して None（過去の議事録に遡って反応しない）。"""
    now = now or reminders.now_jst()
    key = STATE_PREFIX + agent_id
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, key)
        last = db.last_message_id(conn, channel_id) or 0
        if state is None:
            db.set_proactive_state(conn, key, last_checked_message_id=last,
                                   last_run_at=reminders.fmt(now))
            return None
        after = state["last_checked_message_id"] or 0
        rows = db.messages_after(conn, channel_id, after_id=after, limit=80)
        db.set_proactive_state(conn, key,
                               last_checked_message_id=max(after, last),
                               last_run_at=reminders.fmt(now))
    rows = [r for r in rows if r.get("is_bot") and (r.get("content") or "").strip()]
    if not rows:
        return None
    text = "\n\n".join(r["content"] for r in rows)[:MAX_MINUTES_CHARS]
    return {"text": text, "date": _jst_date(rows[0].get("created_at")),
            "header_id": rows[0]["id"], "channel_id": int(channel_id)}


def build_extract_prompt(minutes_text, minutes_date):
    """抽出プロンプト（純粋関数・テスト対象）。"""
    return (
        "以下は社内定例会議の議事録。この中の「担当者つきの未完了TODO」を"
        "すべて抽出して。\n\n"
        "ルール:\n"
        f"- 議事録の投稿日は {minutes_date}。期限の相対表現"
        "（来週金曜・今週中 等）はこの日を基準に YYYY-MM-DD へ正規化する\n"
        "- 「8/29開催前まで」のような表現はその日付（2026-08-29）を期限とする\n"
        "- 「できるだけ早く」「未定」など具体日に解決できないものは due=null\n"
        "- owners は本文中の <@数字> メンションをそのまま抜き出す"
        "（メンションが無いTODOは含めない）\n"
        "- 完了済み・過去の報告・決定事項（✅）は含めない\n"
        "- task は要点だけ簡潔に（60文字以内）\n"
        "- 出力はJSONのみ（説明文・コードブロック不要）:\n"
        '{"items": [{"task": "…", "owners": ["<@123>"], '
        '"due": "2026-08-29", "urgent": false}]}\n\n'
        f"【議事録】\n{minutes_text}"
    )


def parse_extract_response(raw, minutes_date):
    """抽出JSONを検証つきで解釈（純粋関数・テスト対象）。
    Returns: {"items": [追跡対象], "skipped_no_due": 期日なしで見送った件数}
    不正なowners・壊れた日付は安全側（そのitemを捨てる）に倒す。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return {"items": [], "skipped_no_due": 0}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {"items": [], "skipped_no_due": 0}
    items, skipped = [], 0
    for it in (data.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        task = str(it.get("task") or "").strip()[:MAX_TASK_LEN]
        owners = [o for o in (it.get("owners") or [])
                  if isinstance(o, str) and MENTION_RE.fullmatch(o.strip())]
        if not task or not owners:
            continue
        due = it.get("due")
        if not (isinstance(due, str) and DATE_RE.match(due)):
            skipped += 1  # 期日を発明しない: 具体日が無いものは追跡しない
            continue
        if due < minutes_date:
            continue  # 過去日は追跡しない（抽出ミスの安全弁）
        items.append({"task": task, "owners": " ".join(owners),
                      "due_date": due, "urgent": bool(it.get("urgent"))})
    return {"items": items, "skipped_no_due": skipped}


def extract_items(minutes_text, minutes_date, *, model=search.DEFAULT_MODEL,
                  invoke_fn=None):
    """議事録→追跡対象TODO。invoke_fnはテスト差し替え口。"""
    prompt = build_extract_prompt(minutes_text, minutes_date)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=EXTRACT_TIMEOUT_SEC).text)
    return parse_extract_response(fn(prompt), minutes_date)


def save_items(db_path, agent_id, channel_id, source_message_id, items):
    """抽出結果を保存して採番idリストを返す。"""
    now = reminders.fmt(reminders.now_jst())
    ids = []
    with db.connect(db_path) as conn:
        for it in items:
            ids.append(db.add_action_item(
                conn, agent_id=agent_id, source_message_id=source_message_id,
                channel_id=channel_id, task=it["task"], owners=it["owners"],
                due_date=it["due_date"], urgent=it["urgent"], created_at=now))
    return ids


# ------------------------------------------- 定例の前回比較（RM#36）

# 新議事録のTODOと前回からの未完了(open)を突き合わせる類似度。名寄せは
# 保守的に（誤同一視で新タスクを取り逃すより、稀な二重追跡を許容する）
CARRYOVER_SIM = 0.6


def match_carryover(new_items, open_items):
    """新議事録の抽出TODOを持ち越し(open)と名寄せする（純粋関数・テスト対象）。
    Returns: (track_new, carried)
      track_new: 新規として追跡するもの
      carried:   既存openと同一とみなすもの（再追跡しない＝二重声かけ防止）。
                 [{"new": 抽出item, "old": 既存open行}]"""
    track, carried = [], []
    for it in new_items:
        dup = None
        for old in open_items:
            ratio = difflib.SequenceMatcher(
                None, it["task"], old["task"]).ratio()
            if ratio >= CARRYOVER_SIM:
                dup = old
                break
        if dup is None:
            track.append(it)
        else:
            carried.append({"new": it, "old": dup})
    return track, carried


def build_carryover_note(open_items, carried, today):
    """追跡宣言に足す「先週からの持ち越し」行（純粋関数・無ければ空文字）。
    open_items: 今回の議事録より前からの未完了全件。carriedの分は
    「今週も議題に載った」持ち越しとして強調する。"""
    if not open_items:
        return ""
    carried_ids = {c["old"]["id"] for c in carried}
    lines = []
    for it in open_items[:8]:
        overdue = "（期日超過）" if it["due_date"] < today else \
            f"（期日 {it['due_date']}）"
        again = "・今週も議題に" if it["id"] in carried_ids else ""
        lines.append(f"「{it['task'][:40]}」{overdue}{again}")
    note = f"-# ⏳ 先週からの持ち越し{len(open_items)}件: " + "、".join(lines)
    if len(open_items) > 8:
        note += " …"
    return note


# ---------------------------------------------------------------- 2) 追跡宣言

def build_confirmation(items, skipped_no_due):
    """追跡宣言の文面（純粋関数・メンションは鳴らさない前提で整形）。"""
    lines = [f"📋 この議事録から期日つきタスク{len(items)}件を追跡するっス:"]
    for i, it in enumerate(items, 1):
        flag = "🔴 " if it.get("urgent") else ""
        lines.append(f"{i}. {flag}{it['task']}（{it['owners']} ／ "
                     f"期日 {it['due_date']}）")
    if skipped_no_due:
        lines.append(f"-# 期日が具体化されていないTODO {skipped_no_due}件は"
                     "追跡対象外っス（期日が決まったら教えてほしいっス）")
    lines.append("-# 期日2日前と当日朝に担当の人へ声かけするっス。"
                 "誤抽出ならこのメッセージに❌で追跡を取り消せるっス")
    return "\n".join(lines)


def set_confirm_message(db_path, item_ids, message_id):
    with db.connect(db_path) as conn:
        db.set_action_confirm_message(conn, item_ids, message_id)


# ---------------------------------------------------------------- 3) 声かけ

def desired_stage(due_date, today):
    """今日時点で到達しているべき声かけ段階（純粋関数・テスト対象）。
    due_date/today は YYYY-MM-DD 文字列（辞書順比較で正しい）。"""
    if today > due_date:
        return "overdue"
    if today == due_date:
        return "day"
    d = datetime.strptime(due_date, "%Y-%m-%d").date()
    t = datetime.strptime(today, "%Y-%m-%d").date()
    if (d - t).days <= 2:
        return "before"
    return "none"


def items_needing_nudge(db_path, agent_id, today):
    """声かけが必要な (item, stage) のリスト（段階が進んだものだけ）。"""
    out = []
    with db.connect(db_path) as conn:
        for item in db.open_action_items(conn, agent_id):
            want = desired_stage(item["due_date"], today)
            if STAGES.index(want) > STAGES.index(item["nudge_stage"] or "none"):
                out.append((item, want))
    return out[:NUDGE_PER_CYCLE]


def build_nudge_text(item, stage, guild_id):
    """声かけ文面（静的・claude不使用: 定時通知の確実性優先）。"""
    link = search.jump_link(guild_id, item["channel_id"],
                            item["source_message_id"])
    head = {"before": f"期日（{item['due_date']}）が近いっスよ",
            "day": f"今日（{item['due_date']}）が期日っス",
            "overdue": f"期日（{item['due_date']}）を過ぎてるっス"}[stage]
    tail = ("進捗どうっスか？" if stage != "overdue"
            else "状況だけでも教えてほしいっス")
    return (f"⏰ {item['owners']} 定例のタスク「{item['task']}」、{head}。"
            f"{tail}\n"
            f"-# 完了してたらこのメッセージに✅で追跡を終了するっス｜"
            f"元の議事録: {link}")


def record_nudge(db_path, item_id, stage, message_id):
    with db.connect(db_path) as conn:
        db.update_action_nudge(conn, item_id, stage=stage,
                               message_id=message_id)


# ---------------------------------------------------------------- 4) 完了・取消

def complete_by_nudge_message(db_path, message_id):
    """✅リアクションからの完了。対象itemのdict（無ければNone）。"""
    with db.connect(db_path) as conn:
        return db.complete_action_by_nudge_message(conn, message_id)


def drop_by_confirm_message(db_path, message_id):
    """❌リアクションからの一括取り消し。取り消した件数。"""
    with db.connect(db_path) as conn:
        return db.drop_actions_by_confirm_message(conn, message_id)


# ------------------------------------------- 会話スキル（キャンセル/完了）

# 実例2026-08-07: 会話で「不要になった」と言われたアーカイブ担当が「キャンセルするっス」
# と答えたが実行手段が無く、期日超過アラートが出続けた。会話からも
# リアクション（✅/❌）と同じ実挙動に到達できるようマーカーを用意する。
CANCEL_MARKER_RE = re.compile(r"\[ACTION_CANCEL:\s*(\d+)\s*\]")
DONE_MARKER_RE = re.compile(r"\[ACTION_DONE:\s*(\d+)\s*\]")
# 期日変更（2026-08-19）。取消・完了はあるのに変更が無かったため、
# 「金曜にリスケされた」のような日常の予定変更が事実台帳へ流れ込んでいた。
DUE_MARKER_RE = re.compile(
    r"\[ACTION_DUE:\s*(\d+)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\]")

# status → 人間向けの終了ラベル（既終了の⚠️行用）
_CLOSED_LABELS = {"done": "完了済み", "cancelled": "キャンセル済み",
                  "dropped": "取消済み"}


def extract_conversation_markers(answer):
    """回答から ACTION_CANCEL / ACTION_DONE / ACTION_DUE を全て除去し、
    (本文, cancel_ids, done_ids, due_changes) を返す（純粋関数）。
    due_changes は [(id, "YYYY-MM-DD")]。
    リマインダーと同様、パース成否に関わらず生マーカーはchに晒さない。"""
    cancel_ids = [int(x) for x in CANCEL_MARKER_RE.findall(answer or "")]
    done_ids = [int(x) for x in DONE_MARKER_RE.findall(answer or "")]
    due_changes = [(int(i), d)
                   for i, d in DUE_MARKER_RE.findall(answer or "")]
    text = DUE_MARKER_RE.sub(
        "", DONE_MARKER_RE.sub("", CANCEL_MARKER_RE.sub("", answer or "")))
    return text.strip(), cancel_ids, done_ids, due_changes


def format_item_line(item):
    """スキル指示文用の1行表示（純粋関数）。"""
    return (f"id={item['id']}: {item['task'][:60]}"
            f"（期日 {item['due_date']} ／ 担当 {item['owners']}）")


def build_skill_note(open_items):
    """会話スキルの指示文（追跡中一覧を動的注入・純粋関数）。"""
    lines = [f"  {format_item_line(it)}" for it in open_items[:20]]
    listing = "\n".join(lines) if lines else "  （なし）"
    return (
        "【納期追跡スキル】あなたは定例議事録から期日つきタスクを抽出して"
        "追跡し、期日前後に担当へ声かけしている。\n"
        "追跡中のタスク:\n"
        f"{listing}\n"
        "タスクが不要になった・無くなったと言われたら、返信本文の最後に"
        "改行して: [ACTION_CANCEL: id]\n"
        "タスクが終わったと言われたら: [ACTION_DONE: id]\n"
        "**期日が変わった**と言われたら: [ACTION_DUE: id | YYYY-MM-DD]\n"
        "（例:「金曜にリスケされた」→ その週の金曜の日付に変換して指定する。"
        "変更すると声かけ段階がリセットされ、新しい期日で2日前・当日の"
        "声かけが改めて出る）\n"
        "規則:\n"
        "- 上の一覧だけを真実として扱う。一覧に載っているタスクは、過去の"
        "会話で何と言っていようと今も追跡中＝未キャンセル。「済み」と答えて"
        "よいのは一覧に無いときだけ\n"
        "- どのタスクの話か上の一覧から特定できないときは、マーカーを付けず"
        "本文で確認する\n"
        "- 期日の変更はマーカーで実行できる。**期日が動いただけの話を"
        "[FACT:]（事実台帳）に書かないこと**（台帳側の項目なので二重管理に"
        "なり、後で古い情報が残る）\n"
        "- 一覧に無いタスクの操作や、担当の変更はできない。できない依頼に"
        "「やっておく」と言わず、できないと正直に答える\n"
        "- 相対的な日付（「金曜」「来週頭」）は現在日時から絶対日付へ変換する。"
        "どの日か特定できないときはマーカーを付けず本文で確認する\n"
        "- 追跡タスクと関係ない話には、マーカーを絶対に付けないこと"
    )


def _is_owner(item, author_id):
    return bool(re.search(rf"<@!?{int(author_id)}>", item["owners"] or ""))


def apply_conversation_ops(db_path, agent_id, *, author_id, is_admin,
                           cancel_ids, done_ids, due_changes=()):
    """会話マーカーの実行。担当か管理者だけが操作できる。
    Returns: (実行結果の-#行[], 成功した操作[{"id","action","task"}])。
    -#行の接頭辞「納期追跡」は honesty.py の実行証拠パターンと対応。"""
    ops = ([(rid, "cancelled") for rid in cancel_ids]
           + [(rid, "done") for rid in done_ids]
           + [(rid, ("due", due)) for rid, due in (due_changes or ())])
    notes, applied = [], []
    if not ops:
        return notes, applied
    with db.connect(db_path) as conn:
        for rid, status in ops:
            item = db.get_action_item(conn, rid, agent_id)
            if item is None:
                notes.append(f"-# ⚠️ 納期追跡: id={rid} は無いっス")
                continue
            if item["status"] != "open":
                label = _CLOSED_LABELS.get(item["status"], "終了済み")
                notes.append(f"-# ⚠️ 納期追跡: id={rid} は既に{label}っス")
                continue
            if not (is_admin or _is_owner(item, author_id)):
                notes.append(f"-# ⚠️ 納期追跡: id={rid} は担当の人か管理者"
                             "だけが操作できるっス")
                continue
            if isinstance(status, tuple):           # 期日変更
                _, new_due = status
                old = db.reschedule_action_item(conn, rid, agent_id, new_due)
                if old is None:
                    notes.append(f"-# ⚠️ 納期追跡: id={rid} の期日を"
                                 "変更できなかったっス")
                    continue
                notes.append(f"-# 📅 納期追跡の期日を変更(id={rid}): "
                             f"{old} → {new_due}（{item['task'][:30]}）")
                applied.append({"id": rid, "action": "due", "due": new_due,
                                "task": item["task"]})
                continue
            db.close_action_item(conn, rid, agent_id, status=status)
            if status == "cancelled":
                notes.append(f"-# 🗑 納期追跡をキャンセル(id={rid}): "
                             f"{item['task'][:40]}")
            else:
                notes.append(f"-# 📗 納期追跡を完了として記録(id={rid}): "
                             f"{item['task'][:40]}")
            applied.append({"id": rid, "action": ("cancel" if
                            status == "cancelled" else "done"),
                            "task": item["task"]})
    return notes, applied
