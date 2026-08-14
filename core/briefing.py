#!/usr/bin/env python3
"""
朝のブリーフィング（先回り/outward・エージェントv3 / ロードマップ #42）。

毎朝1回、今日の期日・予定・未読の重要事項を1本のメッセージに集約してホームchへ配信する。
「その日の指定時刻（既定8時）以降・まだ配信していなければ1回だけ」送る（毎朝1本）。

三本柱（それぞれ出所が1つに対応する＝集約は決定論で組み立てられる）:
  1) 期日     : 議事録由来の追跡中タスク（action_items）を 超過/今日/近日 に区分する
  2) 予定     : 今日のリマインダー（reminders の active から due が今日のもの）
  3) 重要事項 : 前回配信以降の未読発言を安いモデルが3点まで要約（読むだけ・JSON出力）

安全規約（新しい外向き機能）:
  - フラグ（proactive.briefing.enabled、既定オフ）で「設定1行で止められる」を保証する
  - 既定シャドー（briefing.shadow=true）: 実投稿せず proactive_log に本文を記録するだけ。
    実データでの集約の質を人間が確認してから shadow=false で本投稿を解禁する
  - 集約・整形は決定論（claude を通さない）。LLM を使うのは③の要約だけで、
    失敗しても①②だけで配信する（best-effort・重要事項は「あれば添える」）
  - 配信はメンションを鳴らさない（担当の @ は表示だけ。声かけは納期追跡の領分）

未読の起点（checkpoint）は proactive_state に "briefing:" 接頭辞で同居させる
（minutes:/homework: と同じ流儀）。初回は「今」に初期化し過去は要約しない。

Discordへの投稿・ループは bot.py（_briefing_cycle）が持つ。
単体テスト: ./venv/bin/python -m unittest test_briefing -v
"""

import json
import os
import re
from datetime import datetime, timedelta

from core import invoke_claude
from core import db
from core import reminders
from core import search
# 観察ループのcheckpointと同居させるための専用キー接頭辞（minutes:/homework: と同じ）
STATE_PREFIX = "briefing:"

BRIEFING_HOUR_DEFAULT = 8      # この時刻以降の最初の観察周期で配信する（JST）
SOON_DAYS = 2                  # 「近日」に載せる期日の猶予（今日+この日数先まで）
MAX_DEADLINES = 10            # 1本に載せる期日の上限（毎朝の1本を短く保つ）
MAX_SCHEDULES = 10           # 1本に載せる予定の上限
MAX_TASK_LEN = 60
MAX_CONTENT_LEN = 60
MAX_BRIEFING_CHARS = 1900     # Discord 2000字制限内に収める最終ガード

# 重要事項の要約は「未読を読むだけ」の定型作業なので安いモデルで足りる
HIGHLIGHT_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
HIGHLIGHT_TIMEOUT_SEC = 180
MAX_UNREAD_MESSAGES = 120     # 要約に読む未読発言の上限（残りは切り捨て）
PER_MESSAGE_CHARS = 200       # 要約プロンプトの1発言引用上限
MAX_HIGHLIGHTS = 3            # 重要事項として出す最大件数（1本を短く保つ）
MAX_HIGHLIGHT_LEN = 120

_JSON_RE = re.compile(r"\{.*\}", re.S)


# ---------------------------------------------------------------- 配信タイミング

def due_today(now, last_run_at, hour):
    """今日この時刻以降で、まだ配信していなければ True（純粋関数・テスト対象）。
    last_run_at は前回配信時刻（fmt形式）。同じ日付なら今日はもう送らない。"""
    if now.hour < hour:
        return False
    if last_run_at:
        try:
            if reminders.parse_dt(last_run_at).date() == now.date():
                return False   # 今日はもう配信済み
        except ValueError:
            pass
    return True


def should_send(db_path, agent_id, *, hour=BRIEFING_HOUR_DEFAULT, now=None):
    """今日の配信タイミングに達したか（state を読むだけ・claude不使用）。"""
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    last = state.get("last_run_at") if state else None
    return due_today(now, last, hour)


def mark_sent(db_path, agent_id, checkpoint, now=None):
    """配信（またはシャドー/空配信）後に呼ぶ。last_run_at を今日にして再送を止め、
    未読の起点を checkpoint まで前進させる。"""
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=int(checkpoint),
                               last_run_at=reminders.fmt(now))


# ---------------------------------------------------------------- 集約（IO）

def collect(db_path, agent_id, *, home_channel_id, exclude_channel_ids=(),
            now=None, list_reminders=None):
    """配信データを集める（claude はここでは呼ばない）。
    list_reminders はテスト差し替え口（既定 reminders.list_active）。
    Returns: {"deadlines": {...}, "schedules": [...], "unread": [...],
              "checkpoint": int}。checkpoint は現在の最大message_id。"""
    now = now or reminders.now_jst()
    today = now.strftime("%Y-%m-%d")
    list_reminders = list_reminders or reminders.list_active
    excl = {int(home_channel_id)} | {int(c) for c in exclude_channel_ids or ()}
    with db.connect(db_path) as conn:
        actions = db.open_action_items(conn, agent_id)
        checkpoint = db.max_message_id(conn)
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
        if state is None:
            unread = []   # 初回は過去を遡って要約しない（backlog爆発防止）
        else:
            after = state["last_checked_message_id"] or 0
            unread = db.human_messages_after(
                conn, after, exclude_channel_ids=excl,
                limit=MAX_UNREAD_MESSAGES)
    return {"deadlines": partition_deadlines(actions, today),
            "schedules": reminders_due_today(list_reminders(), today),
            "unread": unread, "checkpoint": checkpoint}


# ---------------------------------------------------------------- ①期日・②予定

def partition_deadlines(items, today):
    """open な action_items を 超過/今日/近日 に区分（純粋関数・テスト対象）。
    近日は today+1〜today+SOON_DAYS。それ以降の期日は載せない
    （毎朝の1本を短く保つ＝遠い期日は納期声かけ side に任せる）。
    items は due_date 昇順（db.open_action_items）想定なので各区分も昇順。"""
    horizon = (datetime.strptime(today, "%Y-%m-%d")
               + timedelta(days=SOON_DAYS)).strftime("%Y-%m-%d")
    overdue, day, soon = [], [], []
    for it in items:
        due = it.get("due_date") or ""
        if not due:
            continue
        if due < today:
            overdue.append(it)
        elif due == today:
            day.append(it)
        elif due <= horizon:
            soon.append(it)
    return {"overdue": overdue, "today": day, "soon": soon}


def reminders_due_today(active, today):
    """active なリマインダーのうち due が今日のものを返す（純粋関数・テスト対象）。
    active は reminders.list_active() の戻り（due 昇順・'YYYY-MM-DDTHH:MM'）。"""
    return [r for r in active if (r.get("due") or "")[:10] == today]


# ---------------------------------------------------------------- ③重要事項

def build_highlight_prompt(messages, agent_name):
    """重要事項の要約プロンプト（純粋関数・テスト対象）。"""
    lines = []
    for m in messages:
        text = (m["content"] or "").strip().replace("\n", " ")
        if len(text) > PER_MESSAGE_CHARS:
            text = text[:PER_MESSAGE_CHARS] + "…"
        lines.append(f"- #{m['channel']} {m['author']}: {text}")
    return (
        f"あなたはチームのチャットを見守るAIエージェント「{agent_name}」。\n"
        "以下は前回の朝から今朝までの、まだ共有されていない社内発言の一覧。"
        "この中から、今朝チームに一言で共有しておく価値がある『重要事項』だけを"
        f"最大{MAX_HIGHLIGHTS}点、短く箇条書きにする。\n\n"
        "原則:\n"
        "- 該当なしが正常。雑談・感想・進行中の細かいやりとりは拾わない\n"
        "- 決定事項・依頼・トラブル・締切など、見落とすと困るものだけを選ぶ\n"
        "- 各項目は誰が何を、が分かる一文で（40〜60文字目安・憶測を足さない）\n\n"
        "出力はJSONのみ（説明文・コードブロック不要）:\n"
        '{"highlights": ["○○さんが△△を決定", "□□の締切が今週金曜"]}\n'
        '該当なしなら {"highlights": []}\n\n'
        "【未読の発言一覧】\n" + "\n".join(lines)
    )


def parse_highlight_response(raw):
    """要約JSONを検証つきで解釈（純粋関数・テスト対象）。
    壊れたJSON・空・非文字列は黙って捨てる（安全側＝重要事項なし）。
    上限は「有効な文字列を数えて」適用する（ゴミ要素で枠を食わせない）。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for h in (data.get("highlights") or []):
        if not isinstance(h, str):
            continue
        s = h.strip().replace("\n", " ")[:MAX_HIGHLIGHT_LEN]
        if s:
            out.append(s)
        if len(out) >= MAX_HIGHLIGHTS:
            break
    return out


def summarize_highlights(messages, *, agent_name,
                         model=HIGHLIGHT_MODEL_DEFAULT, invoke_fn=None):
    """未読発言→重要事項の箇条書き（無ければ空）。invoke_fnはテスト差し替え口。
    要約は best-effort: 失敗しても空を返し、①②だけで配信できるようにする。"""
    if not messages:
        return []
    prompt = build_highlight_prompt(messages, agent_name)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=HIGHLIGHT_TIMEOUT_SEC).text)
    try:
        return parse_highlight_response(fn(prompt))
    except Exception:
        # 要約はあくまで「あれば添える」もの。落ちても期日・予定の配信は止めない
        return []


# ---------------------------------------------------------------- 整形

def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _deadline_line(item, guild_id):
    """期日1件の表示（担当の @ は表示のみ・鳴らさない前提で整形）。"""
    link = search.jump_link(guild_id, item["channel_id"],
                            item["source_message_id"])
    urgent = "🔥" if item.get("urgent") else ""
    return f"{urgent}{_clip(item['task'], MAX_TASK_LEN)}"\
           f"（{item['owners']}）｜{link}"


def _schedule_line(entry):
    """予定1件の表示: '09:00 定例準備（宛先ラベル）'。"""
    t = reminders.parse_dt(entry["due"])
    who = (f"（{entry['mention_label']}）"
           if entry.get("mention_label") else "")
    return (f"{t.hour:02d}:{t.minute:02d} "
            f"{_clip(entry.get('content'), MAX_CONTENT_LEN)}{who}")


def build_briefing(*, date_label, deadlines, schedules, highlights, guild_id):
    """朝のブリーフィング本文（純粋整形・claude不使用・テスト対象）。
    載せる中身が何も無ければ None（空の通知は送らない）。"""
    dl = deadlines or {}
    overdue = dl.get("overdue") or []
    today_items = dl.get("today") or []
    soon = dl.get("soon") or []
    schedules = schedules or []
    highlights = highlights or []
    if not (overdue or today_items or soon or schedules or highlights):
        return None

    lines = [f"☀️ おはようございますっス。{date_label} の朝のブリーフィングっス"]

    if overdue or today_items or soon:
        lines.append("")
        lines.append("📌 今日の期日")
        shown = 0
        for it in overdue:
            if shown >= MAX_DEADLINES:
                break
            lines.append(f"- 🔴 【超過】{_deadline_line(it, guild_id)}")
            shown += 1
        for it in today_items:
            if shown >= MAX_DEADLINES:
                break
            lines.append(f"- 🟠 【今日】{_deadline_line(it, guild_id)}")
            shown += 1
        for it in soon:
            if shown >= MAX_DEADLINES:
                break
            lines.append(
                f"- ⚪ 【{it['due_date']}】{_deadline_line(it, guild_id)}")
            shown += 1
        rest = (len(overdue) + len(today_items) + len(soon)) - shown
        if rest > 0:
            lines.append(f"-# ほか {rest} 件（納期の声かけで個別にお知らせするっス）")

    if schedules:
        lines.append("")
        lines.append("🗓 今日の予定")
        for r in schedules[:MAX_SCHEDULES]:
            lines.append(f"- {_schedule_line(r)}")
        if len(schedules) > MAX_SCHEDULES:
            lines.append(f"-# ほか {len(schedules) - MAX_SCHEDULES} 件")

    if highlights:
        lines.append("")
        lines.append("📰 未読の重要事項")
        for h in highlights:
            lines.append(f"- {h}")

    lines.append("")
    lines.append("-# 今日もいってらっしゃいっス。漏れがあれば気軽に教えてほしいっス")
    text = "\n".join(lines)
    if len(text) > MAX_BRIEFING_CHARS:
        text = text[:MAX_BRIEFING_CHARS] + "…"
    return text
