#!/usr/bin/env python3
"""決定でずれるリマインダーの直し方を作って、✅で反映する。

「定例は10/15から一時的に木曜開催」のような決定が入ったとき、毎週金曜の
リマインダーをどう直すかを、人に考えさせずに案として差し出す。
AI（ripple.py の判定）が読み取るのは「何曜日に・いつから・いつまで・文面」だけで、
具体的な日付はここで計算する（日付の計算を言語モデルに任せない）。

週の単位（月〜日）で置き換える:
- 始まる週より前の元の予定は、そのまま1回ずつ残す（止める前に出るはずだった分）
- 期間中の週は、新しい曜日・同じ時刻で臨時に1回ずつ登録する
- 期間が明けたら、最初の元の曜日から元の繰り返しに戻す
- 期限なし（以降ずっと）なら、繰り返しそのものを新しい曜日へ付け替える
1回きりのリマインダーは、同じ週の新しい曜日へ移す。毎日・毎月は人に任せる。

単体テスト: core/test_reminder_shift.py
"""

import os
import shutil
from datetime import date, datetime, timedelta

from core import reminders

MAX_TEMP = 12                  # 臨時登録の上限（これを超える期間は人が判断する）
WEEKDAYS = "月火水木金土日"
_COPY = ("channel_id", "user_id", "user_name", "mention", "mention_label",
         "channel_label", "agent_id")
_last_backup = None


def _monday(d):
    return d - timedelta(days=d.weekday())


def _date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def plan(rem, *, now, weekday, start, end, content):
    """直し方の案。作れないとき（毎日・毎月・曜日が読めない・期間が長すぎる）は None。"""
    if not isinstance(weekday, int) or not 0 <= weekday <= 6:
        return None
    start_d, end_d = _date(start), (_date(end) if end else None)
    if start_d is None or (end and end_d is None) or (end_d and end_d < start_d):
        return None
    due0 = reminders.parse_dt(rem["due"])
    at = (due0.hour, due0.minute)
    base = {"reminder_id": rem["id"], "repeat": rem["repeat"],
            "label": rem["content"], "weekday": weekday}

    def _add(dt, text, original):
        op = {"op": "add_once", "due": _stamp(dt), "content": text,
              "original": original}
        op.update({k: rem.get(k) for k in _COPY})
        return op

    if rem["repeat"] == "once":
        d = due0.date()
        if d < start_d or (end_d and d > end_d):
            return None
        new = datetime.combine(_monday(d) + timedelta(days=weekday),
                               due0.time())
        if new == due0 or new <= now:
            return None
        return dict(base, ops=[{"op": "set_due", "id": rem["id"],
                                "due": _stamp(new), "content": content or None}])

    if rem["repeat"] != "weekly":
        return None
    week_start = _monday(start_d)
    occ = due0 if due0 > now else reminders.advance_past(due0, "weekly",
                                                         due0.day, now)
    ops = []
    while occ.date() < week_start:                      # 始まる前の週は元どおり
        ops.append(_add(occ, rem["content"], True))
        occ += timedelta(days=7)

    first = datetime.combine(week_start + timedelta(days=weekday),
                             datetime.min.time()).replace(hour=at[0], minute=at[1])
    if first.date() < start_d:
        first += timedelta(days=7)
    if end_d is None:                                   # 以降ずっと＝繰り返しを付け替え
        ops.append({"op": "set_due", "id": rem["id"], "due": _stamp(first),
                    "content": content or None})
        return dict(base, ops=ops)

    week_end = _monday(end_d) + timedelta(days=6)
    temp, cur = [], first
    while cur.date() <= week_end:
        if cur.date() <= end_d and cur > now:
            temp.append(_add(cur, content or rem["content"], False))
        cur += timedelta(days=7)
    if not temp or len(temp) > MAX_TEMP:
        return None
    while occ.date() <= week_end:                       # 期間中の元の予定は出さない
        occ += timedelta(days=7)
    ops += temp
    ops.append({"op": "set_due", "id": rem["id"], "due": _stamp(occ),
                "content": None})
    return dict(base, ops=ops)


def _md(stamp):
    dt = datetime.fromisoformat(stamp)
    return f"{dt.month}/{dt.day}({WEEKDAYS[dt.weekday()]})"


def preview(p):
    """案を人が読める行にする（投稿の本文に使う）。"""
    lines = []
    kept = [o for o in p["ops"] if o["op"] == "add_once" and o.get("original")]
    temp = [o for o in p["ops"] if o["op"] == "add_once" and not o.get("original")]
    moves = [o for o in p["ops"] if o["op"] == "set_due"]
    for o in kept:
        lines.append(f"{_md(o['due'])} {o['due'][11:16]} いつもの文面（1回だけ）")
    if temp:
        days = "・".join(_md(o["due"]) for o in temp)
        lines.append(f"{days} {temp[0]['due'][11:16]}「{temp[0]['content'][:30]}」（臨時）")
    for o in moves:
        if p["repeat"] == "once":
            lines.append(f"{_md(o['due'])} {o['due'][11:16]} に移す")
        elif o["content"] is None and temp:
            lines.append(f"{_md(o['due'])}から元の繰り返しに戻す")
        else:
            wd = WEEKDAYS[datetime.fromisoformat(o["due"]).weekday()]
            text = f"（文面も「{o['content'][:30]}」に）" if o["content"] else ""
            lines.append(f"{_md(o['due'])} {o['due'][11:16]}から毎週{wd}曜に変える{text}")
    return lines


def last_backup():
    return _last_backup


def apply(p, *, now=None):
    """案を反映する。反映前の台帳を .bak に残す（取り消しの手がかり）。"""
    global _last_backup
    now = now or reminders.now_jst()
    if os.path.exists(reminders.STATE_FILE):
        _last_backup = (f"{reminders.STATE_FILE}.ripple-"
                        f"{now.strftime('%Y%m%d%H%M%S')}.bak")
        shutil.copy2(reminders.STATE_FILE, _last_backup)
    done = {"added": 0, "moved": 0}
    for o in p["ops"]:
        if o["op"] == "add_once":
            extra = {"agent_id": o["agent_id"]} if o.get("agent_id") else {}
            entry, _ = reminders.add_reminder(
                o["channel_id"], o["user_id"], o["user_name"], o["content"],
                datetime.fromisoformat(o["due"]), "once",
                mention=o.get("mention"), mention_label=o.get("mention_label"),
                channel_label=o.get("channel_label"), now=now,
                max_active=10_000, **extra)
            done["added"] += entry is not None
        elif o["op"] == "set_due":
            due = datetime.fromisoformat(o["due"])
            if due <= now:
                # ✅が遅れて過ぎた日付に付け替えると、その場で出てしまう
                if p["repeat"] != "weekly":
                    continue
                due = reminders.advance_past(due, "weekly", due.day, now)
            done["moved"] += reminders.set_due(o["id"], _stamp(due), o["content"])
    return done
