#!/usr/bin/env python3
"""決定事項台帳（進化ロードマップ#4）。

「決まったこと」を2経路で蓄積し、自発発言の裏取りの一次資料にする:
  1) 議事録: ✅決定事項の行などをclaudeで構造抽出（bot._minutes_cycle から）
  2) 会話  : 観察ループの一次判定(haiku)が同じ読み取りのついでに検出
             （proactive.screen の decisions 出力 → save_decisions）

台帳は「静かなデータ」＝記録自体は新しい投稿を増やさない。使われる場所は
proactive.decide_reply の【決定事項台帳】ブロックで、①矛盾指摘・③確実情報・
④過去ログ想起の裏取り精度を上げる（FTS検索より一段確度の高い一次資料）。

単体テスト: ./venv/bin/python -m unittest test_decisions -v
"""

import json
import os
import re

from core import invoke_claude
from core import db
from core import reminders
from core import search
EXTRACT_TIMEOUT_SEC = 300
MAX_DECISION_LEN = 200
MAX_TOPIC_LEN = 40
MAX_PER_MINUTES = 30    # 1議事録から記録する上限（暴走防止）
SEARCH_LIMIT = 8        # decide_reply へ注入する台帳エントリの上限
_JSON_RE = re.compile(r"\{.*\}", re.S)


# ---------------------------------------------------------------- 議事録から抽出

def build_minutes_prompt(minutes_text, minutes_date):
    """議事録→決定事項の抽出プロンプト（純粋関数・テスト対象）。"""
    return (
        "以下は社内定例会議の議事録。この中の「決定事項」（✅マークの行や、"
        "明確に決まった・確定したと書かれている事項）をすべて抽出して。\n\n"
        "ルール:\n"
        "- decision は決定内容を1文で簡潔に（背景説明は含めない）\n"
        "- topic は検索用の短い主題（例: グッズ発注 / 月謝交渉）\n"
        "- TODO（これからやる作業）・検討中・個人の意見は含めない\n"
        "- 出力はJSONのみ（説明文・コードブロック不要）:\n"
        '{"decisions": [{"decision": "…", "topic": "…"}]}\n\n'
        f"【議事録（{minutes_date}投稿）】\n{minutes_text}"
    )


def parse_minutes_response(raw):
    """抽出JSONを検証つきで解釈（純粋関数）。壊れていたら空＝安全側。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for d in (data.get("decisions") or [])[:MAX_PER_MINUTES]:
        if not isinstance(d, dict):
            continue
        text = str(d.get("decision") or "").strip()[:MAX_DECISION_LEN]
        if not text:
            continue
        out.append({"decision": text,
                    "topic": str(d.get("topic") or "").strip()[:MAX_TOPIC_LEN]})
    return out


def extract_from_minutes(minutes_text, minutes_date, *,
                         model=search.DEFAULT_MODEL, invoke_fn=None):
    """議事録テキスト→決定事項リスト。invoke_fnはテスト差し替え口。"""
    prompt = build_minutes_prompt(minutes_text, minutes_date)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=EXTRACT_TIMEOUT_SEC).text)
    return parse_minutes_response(fn(prompt))


# ---------------------------------------------------------------- 保存・参照

def save_decisions(db_path, agent_id, items, *, source_kind, channel_id,
                   source_message_id, decided_on):
    """決定事項を台帳へ保存（採番idリスト）。
    items: [{"decision","topic"(,"message_id","channel_id")}]。
    会話由来は item 側の message_id/channel_id が優先される。"""
    now = reminders.fmt(reminders.now_jst())
    ids = []
    with db.connect(db_path) as conn:
        for it in items:
            ids.append(db.add_decision(
                conn, agent_id=agent_id, decision=it["decision"],
                topic=it.get("topic") or "", source_kind=source_kind,
                source_message_id=it.get("message_id") or source_message_id,
                channel_id=it.get("channel_id") or channel_id,
                decided_on=decided_on, created_at=now))
    return ids


def build_ledger_block(db_path, keywords, guild_id):
    """decide_reply 用の【決定事項台帳】ブロック（該当なしなら空文字）。
    各行に出典リンクを添える＝そのまま引用すれば出典ゲートを通る。"""
    with db.connect(db_path) as conn:
        rows = db.search_decisions(conn, keywords, limit=SEARCH_LIMIT)
    if not rows:
        return ""
    lines = []
    for r in rows:
        link = ""
        if r.get("source_message_id") and r.get("channel_id"):
            link = " " + search.jump_link(guild_id, r["channel_id"],
                                          r["source_message_id"])
        topic = f"[{r['topic']}] " if r.get("topic") else ""
        date = f"（{r['decided_on']}決定）" if r.get("decided_on") else ""
        lines.append(f"- {topic}{r['decision']}{date}{link}")
    return ("【決定事項台帳（過去に確定した事項。矛盾指摘・回答の一次資料）】\n"
            + "\n".join(lines))
