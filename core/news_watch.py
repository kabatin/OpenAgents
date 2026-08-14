#!/usr/bin/env python3
"""業界ニュース巡回（進化ロードマップ#39）。

マーケ担当が週1回だけ、社の事業に直接関わるキーワードでWeb検索し、
**3件まで**に絞って報告する。毎朝流すとすぐ読まれなくなるため頻度と件数を
厳しく絞るのが設計の要（ノイズにしない）。

単体テスト: ./venv/bin/python -m unittest test_meta_pack -v
"""

import json
import os
import re

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "news:"
WEEKDAY_DEFAULT = 0     # 月曜
HOUR_DEFAULT = 9
MAX_ITEMS = 3
TIMEOUT_SEC = 300
_JSON_RE = re.compile(r"\{.*\}", re.S)


def should_send(db_path, agent_id, *, weekday=WEEKDAY_DEFAULT,
                hour=HOUR_DEFAULT, now=None):
    now = now or reminders.now_jst()
    if now.weekday() != weekday or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 3:
                return False
        except ValueError:
            pass
    return True


def mark_sent(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def build_prompt(keywords, today):
    return (
        "WebSearchで次のキーワードの直近1週間のニュースを調べて、"
        "社内に共有する価値があるものだけ報告して。\n"
        f"【キーワード】{'、'.join(keywords)}\n"
        f"【今日】{today}\n\n"
        "ルール:\n"
        f"- 最大{MAX_ITEMS}件。該当が無ければ空でよい（無理に埋めない）\n"
        "- 一般論・古い記事・広告記事は除外。1週間以内の具体的な出来事だけ\n"
        "- 各項目に必ず出典URLを付ける\n"
        '- 出力はJSONのみ: {"items": [{"title": "…", "why": "うちに'
        '関係する理由1文", "url": "https://…"}]}'
    )


def parse_items(raw):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in (data.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()[:100]
        url = str(it.get("url") or "").strip()
        if title and url.startswith("http"):
            out.append({"title": title, "url": url[:300],
                        "why": str(it.get("why") or "").strip()[:100]})
    return out


def fetch(keywords, *, model, invoke_fn=None, now=None):
    now = now or reminders.now_jst()
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC,
        allowed_tools=("WebSearch", "WebFetch"),
        allow=("WebSearch", "WebFetch")).text)
    try:
        return parse_items(fn(build_prompt(keywords,
                                           now.strftime("%Y-%m-%d"))))
    except Exception:
        return []


def build_post(items):
    if not items:
        return None
    lines = ["📰 今週の業界ニュースっス（うちに関係ありそうなのだけ）:"]
    for it in items:
        lines.append(f"- **{it['title']}** — {it['why']}\n  {it['url']}")
    lines.append("-# 週1でお届けするっス。要らなければ言ってほしいっス")
    return "\n".join(lines)
