#!/usr/bin/env python3
"""追跡タスクの統一入口。

議事録TODO（action_items）・宿題（homework_items）・リマインダー（reminders.json）を
1つの形に読み替える。テーブルは書き換えない（ビューと振り分けだけ）。
key は種別の頭文字＋id（A6 / H27 / R57）。会話では「H27 を完了に」と言える。

record の形:
    {"key": "A6", "kind": "action|homework|reminder", "id": 6,
     "task": "…", "owner": "<@id> or 名前", "due": "YYYY-MM-DD[THH:MM]",
     "status": "open|asked|asked2|active|…", "stage": "before|day|overdue|…",
     "source_link": "https://discord.com/… or None", "editable": bool}
"""

import re

from core import action_items
from core import db
from core import homework
from core import reminders

KINDS = {"A": "action", "H": "homework", "R": "reminder"}
LABELS = {"action": "議事録TODO", "homework": "宿題", "reminder": "リマインダー"}
ACTIONS = ("done", "cancel", "due", "open")
_KEY_RE = re.compile(r"^\s*([AHR])\s*[-:]?\s*(\d+)\s*$", re.I)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_key(key):
    """'A6' / 'h27' / '6'（旧来の議事録TODO id）→ (kind, id)。不正は None。"""
    if key is None:
        return None
    text = str(key).strip()
    if text.isdigit():
        return "action", int(text)
    m = _KEY_RE.match(text)
    if not m:
        return None
    return KINDS[m.group(1).upper()], int(m.group(2))


def _link(guild_id, channel_id, message_id):
    if channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return None


def list_all(db_path, agent_id, *, actor_id=None, is_admin=False,
             guild_id="0"):
    """3種の追跡を統一の形で返す（期日順）。リマインダーは本人分（管理者は全員）。"""
    rows = []
    with db.connect(db_path) as conn:
        for it in db.open_action_items(conn, agent_id):
            rows.append({
                "key": f"A{it['id']}", "kind": "action", "id": it["id"],
                "task": it["task"], "owner": it.get("owners") or "",
                "due": it.get("due_date") or "", "status": it["status"],
                "stage": it.get("nudge_stage") or "none",
                "source_link": _link(guild_id, it.get("channel_id"),
                                     it.get("source_message_id")),
                "editable": True})
        for it in db.open_homework_items(conn, agent_id):
            rows.append({
                "key": f"H{it['id']}", "kind": "homework", "id": it["id"],
                "task": it["task"], "owner": it.get("owner") or "",
                "due": it.get("follow_up_date") or "", "status": it["status"],
                "stage": it["status"],
                "source_link": _link(guild_id, it.get("channel_id"),
                                     it.get("source_message_id")),
                "editable": True})
    for r in reminders.list_active(None if is_admin else actor_id):
        if r.get("agent_id") not in (None, agent_id):
            continue
        rows.append({
            "key": f"R{r['id']}", "kind": "reminder", "id": r["id"],
            "task": r["content"], "owner": r.get("mention_label")
            or f"@{r.get('user_name') or ''}",
            "due": r["due"], "status": r["status"],
            "stage": r.get("repeat") or "once", "source_link": None,
            "editable": True})
    return sorted(rows, key=lambda x: (x["due"] or "9999", x["key"]))


def format_line(row):
    owner = f" 担当{row['owner']}" if row.get("owner") else ""
    return (f"- {row['key']} [{LABELS[row['kind']]}] 期日{row['due'] or '未定'}"
            f"{owner} {row['task'][:60]}（{row['status']}）")


def _owns_homework(item, actor_id):
    return bool(re.search(rf"<@!?{int(actor_id)}>", item.get("owner") or ""))


def _fail(text):
    return False, [f"-# ⚠️ 納期追跡: {text}"]


def update(db_path, agent_id, key, action, *, due=None, actor_id, is_admin,
           now=None):
    """key の種別に振り分けて更新。Returns: (ok, -#行のリスト)。
    -# 行の書式は honesty.py の「納期追跡」DEEDS に合わせる。"""
    parsed = parse_key(key)
    if parsed is None:
        return _fail(f"「{key}」はタスクの指定として読めません")
    kind, tid = parsed
    action = (action or "").lower()
    if action not in ACTIONS:
        return _fail("操作は done / cancel / due / open のいずれかです")
    if action == "due" and not _DATE_RE.match(due or ""):
        return _fail("期日は YYYY-MM-DD で指定してください")
    if kind == "action":
        return _update_action(db_path, agent_id, tid, action, due, actor_id,
                              is_admin)
    if kind == "homework":
        return _update_homework(db_path, agent_id, tid, action, due,
                                actor_id, is_admin)
    return _update_reminder(tid, action, actor_id, is_admin)


def _update_action(db_path, agent_id, tid, action, due, actor_id, is_admin):
    if action == "open":
        with db.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, task FROM action_items WHERE id=? AND agent_id=?",
                (tid, agent_id)).fetchone()
            if row is None:
                return _fail(f"A{tid} は存在しません")
            if row[0] not in ("stale", "cancelled"):
                return _fail(f"A{tid} は再開できる状態ではありません（{row[0]}）")
            db.set_action_status(conn, tid, agent_id, "open")
        return True, [f"-# 📅 納期追跡を再開(id={tid}): {row[1][:40]}"]
    notes, applied = action_items.apply_conversation_ops(
        db_path, agent_id, author_id=str(actor_id), is_admin=is_admin,
        cancel_ids=[tid] if action == "cancel" else [],
        done_ids=[tid] if action == "done" else [],
        due_changes=[(tid, due)] if action == "due" else ())
    return bool(applied), notes


def _update_homework(db_path, agent_id, tid, action, due, actor_id, is_admin):
    with db.connect(db_path) as conn:
        item = db.get_homework_item(conn, tid, agent_id)
    if item is None:
        return _fail(f"H{tid} は追跡中の宿題にありません")
    if not (is_admin or _owns_homework(item, actor_id)):
        return _fail(f"H{tid} は本人か管理者だけが操作できます")
    if action == "done":
        homework.mark_resolved(db_path, tid)
        return True, [f"-# 📗 納期追跡（宿題 H{tid}）を完了: {item['task'][:40]}"]
    if action == "cancel":
        homework.mark_closed(db_path, tid)
        return True, [f"-# 🗑 納期追跡（宿題 H{tid}）を取消: {item['task'][:40]}"]
    if action == "due":
        with db.connect(db_path) as conn:
            db.set_homework_follow_up(conn, tid, agent_id, due)
        return True, [f"-# 📅 納期追跡（宿題 H{tid}）の確認日を変更: "
                      f"{item['follow_up_date']} → {due}"]
    return _fail(f"宿題 H{tid} に open はありません")


def _update_reminder(tid, action, actor_id, is_admin):
    if action == "due":
        return _fail(f"リマインダー R{tid} の日時変更は、取り消して登録し直して"
                     "ください（cancel_reminder → add_reminder）")
    if action == "open":
        return _fail(f"リマインダー R{tid} は再開できません")
    entry, reason = reminders.cancel_reminder(tid, actor_id, is_admin=is_admin)
    if entry:
        return True, [f"-# 🗑 納期追跡（リマインダー R{tid}）を取消: "
                      f"{entry['content'][:40]}"]
    why = {"not_found": "存在しません", "ended": "既に終了済みです",
           "not_owner": "本人か管理者だけが取り消せます"}.get(reason, reason)
    return _fail(f"R{tid} は{why}")
