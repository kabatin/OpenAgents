#!/usr/bin/env python3
"""
宿題検出（自己コミットの追跡 / エージェントv3 Phase E）。設計: docs/agents-v3-proactive.md

会話の中の「あとでやる」「確認しとく」「調べておく」等、発言者本人が作業を後回しに
する宣言（自己コミットメント）を拾い、数日後に本人へ一度だけ「あれどうなりました?」と
そっと確認する。納期追跡（action_items）の兄弟だが、出所が定例議事録ではなく普段の
会話で、声かけは段階的でなく「数日後に一回」である点が異なる。

三段の流れ（納期追跡と同じ流儀）:
  1) collect_new_messages: 人間の新規発言の差分を集める（決定論）。自分の観察
     checkpoint（homework: 接頭辞）を前進。新規ゼロなら claude を起動しない
  2) detect            : 安いモデルが自己コミットだけを抽出（読むだけ・JSON出力）
  3) （数日後）声かけ  : follow_up 日を過ぎた宿題へ本人に一度だけ確認（静的文面）

原則:
  - 検知自体は沈黙（外向きの投稿を増やさない）。追跡だけして黙っている
  - 外向きなのは声かけだけ。フラグ（既定オフ）＋シャドー（既定オン）で安全化する
    ＝実データでの検知の質を人間が proactive_log で確認してから本投稿を解禁する
  - owner は必ず発言者本人（他人への依頼は宿題ではない）。期日は発明せず
    「検知日＋follow_up_days」で一律に置く（会話の宿題に締切は無いのが普通）
  - 声かけは日次枠と別勘定（proactive_log の hw_* は Phase A の枠に数えない）
  - 遅れすぎた宿題（expire_days 超）は掘り返さない（Bot停止明けの蒸し返し防止）

Discordへの投稿・ループは bot.py（_homework_detect_cycle / _homework_followup_cycle）
が持つ。単体テスト: ./venv/bin/python -m unittest test_homework -v
"""

import json
import os
import re
from datetime import datetime, timedelta

from core import invoke_claude
from core import db
from core import reminders
from core import search
# 一次判定は「後回し宣言かどうか」を読むだけの定型作業なので安いモデルで足りる
SCREEN_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
DETECT_TIMEOUT_SEC = 120
MAX_MESSAGES_PER_CYCLE = 80   # 1周期で読む新規発言の上限（残りは次周期へ）
PER_MESSAGE_CHARS = 300       # 検知プロンプトの1発言引用上限
MAX_CANDIDATES = 5            # 1周期で拾う宿題の上限（暴走防止）
MAX_TASK_LEN = 120

FOLLOW_UP_DAYS_DEFAULT = 3    # 検知から何日後に「あれどうなりました?」と聞くか
EXPIRE_DAYS_DEFAULT = 14      # follow_up 日をこれ以上過ぎたら掘り返さない
FOLLOWUP_PER_CYCLE = 5        # 1周期の声かけ上限（ch連投防止）
FOLLOWUP_HOURS = range(8, 22)  # 声かけしてよい時間帯（JST）

# 観察ループのcheckpointと同居させるための専用キー接頭辞（minutes: と同じ流儀）
STATE_PREFIX = "homework:"

_JSON_RE = re.compile(r"\{.*\}", re.S)


# ---------------------------------------------------------------- 1) 差分収集

def collect_new_messages(db_path, agent_id, *, home_channel_id,
                         exclude_channel_ids=(), now=None):
    """人間の新規発言の差分を集めて checkpoint を前進する。claude はここでは呼ばない。
    初回は「今」に初期化して None（過去に遡って宿題を掘り返さない）。
    Returns: None（初回/新着なし）| {"messages": [...]}。"""
    now = now or reminders.now_jst()
    key = STATE_PREFIX + agent_id
    excl = {int(home_channel_id)} | {int(c) for c in exclude_channel_ids or ()}
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, key)
        max_id = db.max_message_id(conn)
        if state is None:
            db.set_proactive_state(conn, key, last_checked_message_id=max_id,
                                   last_run_at=reminders.fmt(now))
            return None
        after = state["last_checked_message_id"] or 0
        msgs = db.human_messages_after(conn, after, exclude_channel_ids=excl,
                                       limit=MAX_MESSAGES_PER_CYCLE)
        if len(msgs) >= MAX_MESSAGES_PER_CYCLE:
            new_ckpt = msgs[-1]["id"]   # 上限到達: 消化分まで前進し残りは次周期
        else:
            new_ckpt = max_id
        db.set_proactive_state(conn, key,
                               last_checked_message_id=max(after, new_ckpt),
                               last_run_at=reminders.fmt(now))
        if not msgs:
            return None
    return {"messages": msgs}


# ---------------------------------------------------------------- 2) 検知

def build_detect_prompt(messages, agent_name):
    """検知プロンプト（純粋関数・テスト対象）。"""
    lines = []
    for m in messages:
        text = (m["content"] or "").strip().replace("\n", " ")
        if len(text) > PER_MESSAGE_CHARS:
            text = text[:PER_MESSAGE_CHARS] + "…"
        lines.append(f"- id={m['id']} #{m['channel']} {m['author']}: {text}")
    return (
        f"あなたはチームのチャットを見守るAI「{agent_name}」の観察係。\n"
        "以下の発言の中から、発言者が「自分が後でやる」と口にした宿題"
        "（自己コミットメント）だけを拾う。数日後に「あれどうなりました?」と"
        "そっと確認するための検知。\n\n"
        "拾うのはこういう発言だけ:\n"
        "- 「あとでやる」「後で確認する」「確認しとく」「調べておく」"
        "「次までにやっておく」等、発言者本人が未完了の作業を後回しにする宣言\n\n"
        "拾わないもの（迷ったら拾わない。誤った掘り返しは信頼を失う）:\n"
        "- すでに終わった報告・完了形（「やった」「確認済み」）\n"
        "- 他人への依頼・指示（本人の宿題ではない）\n"
        "- 「明日9時に」など具体的な日時を伴う予定（リマインダーの領分）\n"
        "- 雑談・感想・一般論・仮定の話\n\n"
        f"- 拾うのは最大{MAX_CANDIDATES}件。task は何をやると言ったかを"
        "簡潔に（40文字以内・発言者視点で）\n\n"
        "出力はJSONのみ（説明文・コードブロック不要）:\n"
        '{"commitments": [{"message_id": 123, "task": "見積もりの確認"}]}\n'
        '該当なしなら {"commitments": []}\n\n'
        "【発言一覧】\n" + "\n".join(lines)
    )


def parse_detect_response(raw, valid_ids):
    """検知JSONを検証つきで解釈（純粋関数・テスト対象）。
    壊れたJSON・未知のid・task欠落・重複は黙って捨てる（安全側＝沈黙）。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out, seen = [], set()
    for c in (data.get("commitments") or [])[:MAX_CANDIDATES]:
        if not isinstance(c, dict):
            continue
        try:
            mid = int(c.get("message_id"))
        except (TypeError, ValueError):
            continue
        task = str(c.get("task") or "").strip()[:MAX_TASK_LEN]
        if mid not in valid_ids or mid in seen or not task:
            continue
        seen.add(mid)
        out.append({"message_id": mid, "task": task})
    return out


def detect(messages, *, agent_name, model=SCREEN_MODEL_DEFAULT,
           invoke_fn=None):
    """検知: 自己コミット候補のリストを返す（無ければ空）。invoke_fnはテスト差し替え口。"""
    prompt = build_detect_prompt(messages, agent_name)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=DETECT_TIMEOUT_SEC).text)
    return parse_detect_response(fn(prompt), {m["id"] for m in messages})


# ---------------------------------------------------------------- 3) 保存

def follow_up_date(committed_date, days):
    """committed_date(YYYY-MM-DD) の days 日後（純粋関数・テスト対象）。"""
    base = datetime.strptime(committed_date, "%Y-%m-%d")
    return (base + timedelta(days=int(days))).strftime("%Y-%m-%d")


def save_commitments(db_path, agent_id, candidates, messages, *,
                     follow_up_days=FOLLOW_UP_DAYS_DEFAULT, now=None):
    """検知結果を保存する。owner=発言者本人・期日=検知日+follow_up_days。
    保存できた宿題dictのリストを返す（同一発言の重複追跡はDB側で無視）。"""
    now = now or reminders.now_jst()
    today = now.strftime("%Y-%m-%d")
    follow = follow_up_date(today, follow_up_days)
    created = reminders.fmt(now)
    by_id = {m["id"]: m for m in messages}
    saved = []
    with db.connect(db_path) as conn:
        for c in candidates:
            msg = by_id.get(c["message_id"])
            if not msg or not msg.get("author_id"):
                continue  # 発言者不明の宿題は追わない（誰に聞くか決められない）
            owner = f"<@{msg['author_id']}>"
            iid = db.add_homework_item(
                conn, agent_id=agent_id, source_message_id=c["message_id"],
                channel_id=msg["channel_id"], owner=owner, task=c["task"],
                committed_date=today, follow_up_date=follow,
                created_at=created)
            if iid is not None:
                saved.append({"id": iid, "owner": owner, "task": c["task"],
                              "channel_id": msg["channel_id"],
                              "follow_up_date": follow})
    return saved


# ---------------------------------------------------------------- 4) 声かけ

def followup_action(follow_up_date, today, expire_days=EXPIRE_DAYS_DEFAULT):
    """今日この宿題をどう扱うか（純粋関数・テスト対象。YYYY-MM-DD の辞書順でOK）。
    'wait'=まだ先 / 'ask'=声かけ時 / 'expire'=遅れすぎで掘り返さない。"""
    if today < follow_up_date:
        return "wait"
    d = datetime.strptime(follow_up_date, "%Y-%m-%d").date()
    t = datetime.strptime(today, "%Y-%m-%d").date()
    if (t - d).days > int(expire_days):
        return "expire"
    return "ask"


def items_needing_followup(db_path, agent_id, today, *,
                           expire_days=EXPIRE_DAYS_DEFAULT,
                           limit=FOLLOWUP_PER_CYCLE):
    """(声かけ対象[], 期限切れで捨てるid[]) を返す。open かつ期日到来分のみ。"""
    ask, expire = [], []
    with db.connect(db_path) as conn:
        due = db.open_homework_due(conn, agent_id, today)
    for item in due:
        act = followup_action(item["follow_up_date"], today, expire_days)
        if act == "ask" and len(ask) < limit:
            ask.append(item)
        elif act == "expire":
            expire.append(item["id"])
    return ask, expire


def build_followup_text(item, guild_id):
    """声かけ文面（静的・claude不使用: 定時通知の確実性優先＝納期声かけと同じ流儀）。"""
    link = search.jump_link(guild_id, item["channel_id"],
                            item["source_message_id"])
    return (f"💭 {item['owner']} そういえば「{item['task']}」、あれから"
            f"どうなりましたっス？（{item['committed_date']} に「あとでやる」"
            "って言ってたやつっス）\n"
            f"-# 余計なお世話だったらスルーで大丈夫っス｜元の発言: {link}")


def mark_asked(db_path, item_id, message_id):
    """声かけ済み（終端）にする。同じ宿題を二度は掘り返さない。"""
    with db.connect(db_path) as conn:
        db.set_homework_status(conn, item_id, "asked",
                               followup_message_id=message_id)


def mark_expired(db_path, item_ids):
    """遅れすぎた宿題を expired にする（声かけしない）。"""
    if not item_ids:
        return
    with db.connect(db_path) as conn:
        for iid in item_ids:
            db.set_homework_status(conn, iid, "expired")
