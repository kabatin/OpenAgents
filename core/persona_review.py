#!/usr/bin/env python3
"""人格・プロンプトの自己リファクタ提案（RM#25）＋スキル使用統計（RM#23）。

毎月1日、各エージェントの行動データ（教訓・訂正・的中率・自動抑制）から
「人格・プロンプトの改善案」をLLMに提案させて相談室へ投稿する。
**適用は人間**（personasは人間管理の一線を守る＝提案まで）。
あわせて使われていないプラグインの棚卸し（retire候補）も同じ投稿で報告する。

単体テスト: ./venv/bin/python -m unittest test_batch_pack -v
"""

import os
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "personareview:"
DAY_DEFAULT = 1
HOUR_DEFAULT = 12
REVIEW_TIMEOUT_SEC = 180
UNUSED_DAYS = 60


def should_send(db_path, agent_id, *, day=DAY_DEFAULT, hour=HOUR_DEFAULT,
                now=None):
    now = now or reminders.now_jst()
    if now.day != day or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 20:
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


def collect(db_path, agent_ids, since):
    """各エージェントの行動データ＋未使用プラグイン一覧。"""
    out = {"agents": {}, "unused_tools": []}
    with db.connect(db_path) as conn:
        for aid in agent_ids:
            out["agents"][aid] = {
                "lessons": db.recent_proactive_lessons(conn, aid, limit=5),
                "hit": db.proactive_hit_stats(conn, aid),
                "corrections": db.count_correction_rules_since(conn, since),
            }
        used = set()
        for (detail,) in conn.execute(
                """SELECT detail FROM proactive_log
                   WHERE kind='plugin' AND action='used'
                     AND created_at >= ?""", (since,)).fetchall():
            used.update((detail or "").split(","))
        for t in db.list_tools(conn):
            if t["status"] == "active" and t["marker"] not in used:
                out["unused_tools"].append(t["name"])
    return out


def build_prompt(data, names):
    lines = []
    for aid, d in data["agents"].items():
        hit = d["hit"]
        lines.append(
            f"### {names.get(aid, aid)}\n"
            f"- 自発発言: {hit['spoke']}件（👍{hit['up']}・👎{hit['down']}）\n"
            f"- 訂正されたルール: {d['corrections']}件\n"
            "- 👎がついた発言例:\n"
            + ("\n".join(f"  ・{l_['text'][:80]}" for l_ in d["lessons"])
               or "  ・なし"))
    return (
        "社内AIエージェントたちの1ヶ月の行動データから、各エージェントの"
        "人格・プロンプト（persona設定）の改善案を提案して。\n\n"
        + "\n".join(lines) + "\n\n"
        "ルール:\n"
        "- データから根拠が読み取れる提案だけ（憶測の性格改造はしない）\n"
        "- 各エージェント最大2件・全体で5件まで。無ければ「提案なし」\n"
        "- 「〜のpersonaに『…』の一文を足す/削る」の粒度で具体的に\n"
        "- 箇条書きのテキストで出力（JSONではない）"
    )


def review(db_path, agent_ids, names, *, model, invoke_fn=None, now=None):
    """月次レビュー実行。(提案テキスト, 未使用ツール一覧)。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=30))
    data = collect(db_path, agent_ids, since)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=REVIEW_TIMEOUT_SEC).text)
    try:
        proposals = (fn(build_prompt(data, names)) or "").strip()[:1200]
    except Exception:
        proposals = ""
    return proposals, data["unused_tools"]


def build_post(proposals, unused_tools):
    lines = ["🪞 月次の自己点検レポートっス（人格チューニングの提案。"
             "**適用するかは管理者判断**っス）"]
    lines.append(proposals or "今月は提案なしっス（大きな問題は見えなかったっス）")
    if unused_tools:
        lines.append("-# 🧰 60日間使われていないプラグイン: "
                     + "、".join(unused_tools[:10])
                     + "（要らなければretireを検討っス）")
    return "\n".join(lines)
