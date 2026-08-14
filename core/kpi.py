#!/usr/bin/env python3
"""自己KPI宣言（RM#87）＋誠実失敗率の自己測定（RM#88の一部）。

四半期の初日に、前四半期の実績を振り返り「次の四半期の目標」を自分で宣言する。
数字は決定論で集計し、宣言文だけLLMが書く（目標の設定自体は自己申告＝
物差しの所有権は人間のまま。宣言は透明性のための可視化）。

単体テスト: ./venv/bin/python -m unittest test_batch_pack2 -v
"""

import os
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "kpi:"
QUARTER_MONTHS = (1, 4, 7, 10)
DAY_DEFAULT = 1
HOUR_DEFAULT = 13
DECLARE_TIMEOUT_SEC = 120


def should_send(db_path, agent_id, *, day=DAY_DEFAULT, hour=HOUR_DEFAULT,
                now=None):
    now = now or reminders.now_jst()
    if now.month not in QUARTER_MONTHS or now.day != day or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 60:
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


def collect(db_path, agent_id, now=None):
    """四半期の実績（決定論）。誠実失敗率＝起票/(起票+できたフリ検出)。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=90))
    with db.connect(db_path) as conn:
        hit = db.proactive_hit_stats(conn, agent_id)
        caps = conn.execute(
            "SELECT COUNT(*) FROM capability_requests WHERE created_at >= ?",
            (since,)).fetchone()[0]
        fake = db.count_proactive_log_since(conn, "fake_done", "caught",
                                            since)
        golden = db.count_golden(conn)
    honest = (round(100 * caps / (caps + fake)) if (caps + fake) else None)
    return {"hit": hit, "capability_requests": caps, "fake_done": fake,
            "honesty_rate": honest, "golden": golden}


def build_prompt(name, stats, quarter_label):
    hit = stats["hit"]
    rate = (round(100 * hit["up"] / hit["spoke"]) if hit["spoke"] else None)
    return (
        f"あなたは社内AIエージェント「{name}」。{quarter_label}の目標を"
        "自分の言葉で宣言して。\n\n"
        f"【前四半期の実績】\n"
        f"- 自発発言 {hit['spoke']}件（👍{hit['up']}・👎{hit['down']}"
        + (f"・👍率{rate}%" if rate is not None else "") + ")\n"
        f"- 能力不足の起票 {stats['capability_requests']}件 / "
        f"できたフリ検出 {stats['fake_done']}件"
        + (f"（誠実失敗率 {stats['honesty_rate']}%）"
           if stats["honesty_rate"] is not None else "") + "\n"
        f"- ゴールデンセット 累計{stats['golden']}問\n\n"
        "ルール: 3行以内・具体的な数字目標を1〜2個・あなたの口調で。"
        "「できないことは正直に言う」姿勢を必ず含める。前置き不要。"
    )


def declare(db_path, agent_id, name, *, model, invoke_fn=None, now=None):
    """(宣言文, 実績dict)。生成失敗時は宣言文 None（実績だけ出す）。"""
    now = now or reminders.now_jst()
    stats = collect(db_path, agent_id, now)
    q = (now.month - 1) // 3 + 1
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=DECLARE_TIMEOUT_SEC).text)
    try:
        text = (fn(build_prompt(name, stats, f"{now.year}年Q{q}")) or "").strip()
    except Exception:
        text = ""
    return (text[:600] or None), stats


def build_post(name, declaration, stats, quarter_label):
    hit = stats["hit"]
    rate = (f"{round(100 * hit['up'] / hit['spoke'])}%"
            if hit["spoke"] else "—")
    lines = [f"🎯 {name}の{quarter_label}目標宣言っス",
             declaration or "（今期も、正直に・役に立つことを積み重ねるっス）",
             f"-# 前期実績: 自発発言{hit['spoke']}件（👍率{rate}）"
             f"・起票{stats['capability_requests']}件"
             + (f"・誠実失敗率{stats['honesty_rate']}%"
                if stats["honesty_rate"] is not None else "")]
    return "\n".join(lines)
