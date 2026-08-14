#!/usr/bin/env python3
"""
リマインダー機能のロジック（bot.py から呼ばれる）。

仕組み:
- LLMがリマインド依頼と判断すると回答末尾にマーカーを出力する
    [REMIND: 2026-07-04T09:00 | once | ゴミ出し]
    [REMIND: 2026-07-05T12:00 | once | to=@チーム | エントリー締切]
    [REMIND_CANCEL: 3]
- bot.py がマーカーを本文から除去して本モジュールで登録/キャンセルし、
  結果を Discord の -# サブテキスト行で明示する
- 配信は bot.py の _reminder_loop が due_reminders() を30秒ごとに確認

規約:
- 日時は naive JST の "YYYY-MM-DDTHH:MM" 文字列（JSTはDSTなしなのでnaiveで安全）
- 状態は reminders.json（アトミック書込、破損時は .bak 退避で空から再開）
- 各APIは load→mutate→save を同期で完結させる（内部にawaitなし）。
  bot.py は1プロセス1イベントループなのでロック不要

単体テスト: ./venv/bin/python -m unittest discover -v
"""

import calendar
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from core import paths

STATE_FILE = paths.REMINDERS_PATH

JST = ZoneInfo("Asia/Tokyo")
REPEATS = ("once", "daily", "weekly", "monthly", "monthly_end")
REPEAT_LABELS = {"once": "一回", "daily": "毎日", "weekly": "毎週",
                 "monthly": "毎月", "monthly_end": "毎月末"}
WEEKDAYS = "月火水木金土日"

PAST_GRACE_MIN = 10   # 過去日時でもこの分数以内なら「今due」として登録
MAX_FAIL = 5          # 配信失敗がこの回数に達したら status="error" で再試行停止
KEEP_INACTIVE = 50    # 非activeエントリの保持件数（肥大防止）
MAX_PER_TICK = 15     # 配信ループ1周の最大送信数（bot.py が参照）
MAX_ACTIVE_PER_USER = 30  # 1ユーザーのアクティブ上限（マーカー量産の抑止）

REMIND_MARKER_RE = re.compile(r"\[REMIND:\s*([^\]]+)\]")
CANCEL_MARKER_RE = re.compile(r"\[REMIND_CANCEL:\s*(\d+)\s*\]")
# LLMの揺れに寛容: 区切りが T でも空白でも、秒が付いていても受ける
_DT_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})")


# ---------------------------------------------------------------- 時刻

def now_jst():
    """現在時刻（naive JST）。テストでは now 引数の注入で固定する。"""
    return datetime.now(JST).replace(tzinfo=None)


def parse_dt(s):
    m = _DT_RE.search(s or "")
    if not m:
        raise ValueError(f"日時を解釈できない: {s!r}")
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)))


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def fmt_human(dt):
    return (f"{dt.month:02d}/{dt.day:02d}({WEEKDAYS[dt.weekday()]}) "
            f"{dt.hour:02d}:{dt.minute:02d}")


# ---------------------------------------------------------------- パース

def parse_payload(payload):
    """'日時 | repeat | to=宛先 | 内容' をパース（repeat / to= は省略可）。
    返り値 {"due", "repeat", "to", "content"}。不正は ValueError。"""
    head, _, rest = (payload or "").partition("|")
    due = parse_dt(head)
    rest = rest.strip()

    repeat = "once"
    head, sep, tail = rest.partition("|")
    if head.strip() in REPEATS:
        repeat = head.strip()
        rest = tail.strip() if sep else ""

    to = None
    head, sep, tail = rest.partition("|")
    if head.strip().startswith("to="):
        to = head.strip()[3:].strip().lstrip("@")
        rest = tail.strip() if sep else ""

    if not rest:
        raise ValueError(f"リマインド内容がない: {payload!r}")
    return {"due": due, "repeat": repeat, "to": to or None, "content": rest}


def extract_markers(answer):
    """回答から REMIND / REMIND_CANCEL マーカーを全て除去し、
    (本文, 登録リクエスト[], キャンセルid[], パースエラー文[]) を返す。
    マーカーはパース成否に関わらず必ず除去する（生マーカーをchに晒さない）。"""
    adds = []
    errors = []
    for m in REMIND_MARKER_RE.finditer(answer or ""):
        try:
            adds.append(parse_payload(m.group(1)))
        except ValueError as e:
            errors.append(str(e))
    cancel_ids = [int(x) for x in CANCEL_MARKER_RE.findall(answer or "")]
    text = CANCEL_MARKER_RE.sub("", REMIND_MARKER_RE.sub("", answer or ""))
    return text.strip(), adds, cancel_ids, errors


# ---------------------------------------------------------------- 繰り返し

def next_occurrence(due, repeat, anchor_day):
    """1ステップ先の予定時刻。monthlyは anchor_day を月末にクランプ
    （1/31→2/28→3/31 と復帰する）。monthly_end は常に翌月の最終日。"""
    if repeat == "daily":
        return due + timedelta(days=1)
    if repeat == "weekly":
        return due + timedelta(days=7)
    if repeat in ("monthly", "monthly_end"):
        y, m = (due.year + 1, 1) if due.month == 12 else (due.year, due.month + 1)
        last = calendar.monthrange(y, m)[1]
        day = last if repeat == "monthly_end" else min(anchor_day, last)
        return due.replace(year=y, month=m, day=day)
    raise ValueError(f"不明なrepeat: {repeat!r}")


def advance_past(due, repeat, anchor_day, now):
    """本来の予定時刻を起点に、now を超えるまで前進させる。
    （遅延配信後の次回が配信実時刻基準でずれていくのを防ぐ）"""
    while due <= now:
        due = next_occurrence(due, repeat, anchor_day)
    return due


# ---------------------------------------------------------------- 状態

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if (isinstance(data, dict) and isinstance(data.get("next_id"), int)
                and isinstance(data.get("reminders"), list)):
            return data
        raise ValueError("スキーマ不一致")
    except FileNotFoundError:
        return {"next_id": 1, "reminders": []}
    except (OSError, ValueError) as e:
        # 全リマインダーの黙殺は事故: .bak に退避して大きくログしてから空で再開
        print(f"reminders.json が読めないため {STATE_FILE}.bak に退避: {e}")
        try:
            os.replace(STATE_FILE, STATE_FILE + ".bak")
        except OSError:
            pass
        return {"next_id": 1, "reminders": []}


def _save_state(state):
    active = [r for r in state["reminders"] if r["status"] == "active"]
    inactive = [r for r in state["reminders"] if r["status"] != "active"]
    kept = sorted(active + inactive[-KEEP_INACTIVE:], key=lambda r: r["id"])
    data = {"next_id": state["next_id"], "reminders": kept}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def add_reminder(channel_id, user_id, user_name, content, due, repeat,
                 agent_id="agent1", mention=None, mention_label=None,
                 channel_label=None, now=None):
    """登録して (entry, None) を返す。拒否時は (None, エラー文)。
    - 過去のonce: PAST_GRACE_MIN 分以内なら「今due」として登録、超は拒否
    - 過去のrepeat: 未来の本来時刻へ前進して登録（「毎朝9時」を夜に頼んだ場合）"""
    now = now or now_jst()
    anchor_day = due.day
    if repeat == "once":
        if due < now - timedelta(minutes=PAST_GRACE_MIN):
            return None, f"{fmt_human(due)} は過去のため登録できない"
    elif due <= now:
        due = advance_past(due, repeat, anchor_day, now)

    state = _load_state()
    mine = [r for r in state["reminders"]
            if r["status"] == "active" and r["user_id"] == str(user_id)]
    if len(mine) >= MAX_ACTIVE_PER_USER:
        return None, f"アクティブな登録が上限（{MAX_ACTIVE_PER_USER}件）に達している"
    entry = {
        "id": state["next_id"],
        "channel_id": str(channel_id),
        "user_id": str(user_id),
        "user_name": user_name,
        "content": content,
        "due": fmt(due),
        "repeat": repeat,
        "anchor_day": anchor_day,
        "mention": mention,
        "mention_label": mention_label,
        "channel_label": channel_label,
        "status": "active",
        "fail_count": 0,
        "created_at": fmt(now),
        "agent_id": agent_id,
    }
    state["reminders"].append(entry)
    state["next_id"] += 1
    _save_state(state)
    return entry, None


def cancel_reminder(rid, user_id, is_admin=False):
    """本人（または管理者）のリマインダーをキャンセル。
    Returns: (entry or None, 失敗理由コード or None)
      not_found  : そのidは存在しない
      ended      : 既に配信済み/取消済み
      not_owner  : 他人の所有（管理者ではない依頼者）
    誠実な失敗: 理由を区別して返す（「見つからないか終了済み」で権限不一致を
    ごまかさない。2026-08-07の実事故対応）。"""
    state = _load_state()
    for r in state["reminders"]:
        if r["id"] != rid:
            continue
        if r["status"] != "active":
            return None, "ended"
        if r["user_id"] != str(user_id) and not is_admin:
            return None, "not_owner"
        r["status"] = "cancelled"
        _save_state(state)
        return r, None
    return None, "not_found"


def find_entry(rid):
    """id指定で1件（statusを問わない・診断用）。無ければ None。"""
    state = _load_state()
    return next((r for r in state["reminders"] if r["id"] == rid), None)


def list_active(user_id=None):
    """アクティブなリマインダー（due昇順）。user_id指定で本人分のみ。"""
    state = _load_state()
    rows = [r for r in state["reminders"] if r["status"] == "active"
            and (user_id is None or r["user_id"] == str(user_id))]
    return sorted(rows, key=lambda r: r["due"])


def due_reminders(now=None, agent_id=None):
    """配信すべきリマインダー（due <= now、古い順）。
    agent_id指定でそのエージェント所有分のみ（複数体制での多重配信防止）。"""
    now = now or now_jst()
    return [r for r in list_active() if parse_dt(r["due"]) <= now
            and (agent_id is None or r.get("agent_id") == agent_id)]


def mark_fired(rid, now=None):
    """配信成功後に呼ぶ。once→done、繰り返し→dueを本来時刻基準で前進。"""
    now = now or now_jst()
    state = _load_state()
    for r in state["reminders"]:
        if r["id"] == rid and r["status"] == "active":
            if r["repeat"] == "once":
                r["status"] = "done"
            else:
                due_dt = parse_dt(r["due"])
                # anchor_day欠落の旧エントリは due の日を採用（1日に化けさせない）
                anchor = r.get("anchor_day") or due_dt.day
                r["due"] = fmt(advance_past(due_dt, r["repeat"], anchor, now))
                r["fail_count"] = 0
            _save_state(state)
            return r
    return None


def mark_failed(rid):
    """配信失敗時に呼ぶ。MAX_FAIL 回で error にして無限再試行を止める。"""
    state = _load_state()
    for r in state["reminders"]:
        if r["id"] == rid and r["status"] == "active":
            r["fail_count"] = r.get("fail_count", 0) + 1
            if r["fail_count"] >= MAX_FAIL:
                r["status"] = "error"
            _save_state(state)
            return r
    return None


# ---------------------------------------------------------------- プロンプト

def format_entry_line(entry):
    """一覧・-#行用の1行表示: 'id=3: 07/06(月) 10:00 毎週 定例準備'"""
    due = parse_dt(entry["due"])
    label = REPEAT_LABELS.get(entry["repeat"], entry["repeat"])
    to = (f" 宛先:{entry['mention_label']}"
          if entry.get("mention_label") else "")
    if entry.get("channel_label"):
        to = f" →{entry['channel_label']}" + to
    content = entry["content"]
    if len(content) > 40:
        content = content[:40] + "…"
    return f"id={entry['id']}: {fmt_human(due)} {label}{to} {content}"


def build_skill_note(now, user_entries, all_entries=None):
    """スキル指示文（現在日時＋本人の登録一覧を動的注入）。
    all_entries: 管理者のときだけ渡される全員分（所有者名つきで一覧できる＝
    他人の分の確認・取り消し依頼に正しいidで応えられる）。"""
    lines = [f"  {format_entry_line(e)}" for e in user_entries[:10]]
    listing = "\n".join(lines) if lines else "  （なし）"
    admin_block = ""
    if all_entries is not None:
        others = [e for e in all_entries
                  if e["id"] not in {u["id"] for u in user_entries}]
        olines = [f"  {format_entry_line(e)}（{e['user_name']}さんの分）"
                  for e in others[:30]]
        admin_block = ("あなたの依頼者は管理者なので、全員分のリマインダーを"
                       "確認・キャンセルできる。他の人の登録:\n"
                       + ("\n".join(olines) if olines else "  （なし）")
                       + "\n")
    return (
        "【リマインダースキル】あなたはリマインダーを設定・キャンセルできる。\n"
        f"現在日時: {now.year}-{now.month:02d}-{now.day:02d}"
        f"({WEEKDAYS[now.weekday()]}) {now.hour:02d}:{now.minute:02d} JST\n"
        "依頼者の登録済みリマインダー:\n"
        f"{listing}\n"
        f"{admin_block}"
        "リマインド設定の依頼と判断したら、返信本文の最後に改行して"
        "次の形式の行を追加すること:\n"
        "[REMIND: YYYY-MM-DDTHH:MM | once|daily|weekly|monthly|monthly_end | 内容]\n"
        "キャンセル依頼なら: [REMIND_CANCEL: id]\n"
        "規則:\n"
        "- 「明日」「来週金曜」等は現在日時を基準に絶対日時へ変換する（過去日時は禁止）\n"
        "- 時刻の指定がなければ 09:00 とする\n"
        "- monthly_end は毎月月末発火。内容に角かっこ [ ] は使わない\n"
        "- 通知は依頼者本人にメンションされる。別の人・ロール・全員宛を頼まれた"
        "場合のみ、内容の前に to=名前 を挟む"
        "（例: [REMIND: 2026-07-05T12:00 | once | to=チーム | エントリー締切]。"
        "全員宛は to=everyone）\n"
        "- 複数人に通知する依頼は、to= にカンマ区切りで並べる"
        "（例: to=山田,田中）。名前は相手が言った表記のままでよい"
        "（登録済みの対応表で解決される）\n"
        "- 別のチャンネルへ通知してほしい依頼は、to= に #チャンネル名 を入れる"
        "（例: [REMIND: 2026-07-10T10:00 | weekly | to=#金曜定例 | YT定例]。"
        "人と併記可: to=#金曜定例,山田）\n"
        "- 登録済み一覧を聞かれたら上の一覧をもとに本文で普通に答える（マーカー不要）\n"
        "リマインド関連の依頼でない相談には、マーカーを絶対に付けないこと。"
    )
