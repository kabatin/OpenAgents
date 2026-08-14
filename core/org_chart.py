#!/usr/bin/env python3
"""組織図の自動更新（RM#65）＋人格の定期健診（RM#73）。

#65: 人間メンバー（profiles由来）＋AIメンバー（config/台帳）を1枚の
     組織図にまとめ、ホームchへ月次投稿する。既に投稿済みなら編集で
     上書きする（チャンネルに同じ図が積み上がらない）。
#73: 各AIの人格定義（personas）と、実際の発言（直近サンプル）を突き合わせ、
     「宣言している人格」と「実際の振る舞い」の乖離をLLMが指摘する。
     提案のみ（personasの書き換えは人間）。

単体テスト: ./venv/bin/python -m unittest test_org_chart -v
"""

import os
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
CHART_STATE = "orgchart:"
CHECKUP_STATE = "checkup:"
DAY_DEFAULT = 1
CHART_HOUR = 14
CHECKUP_HOUR = 15
MAX_HUMANS = 20
SAMPLE_MESSAGES = 12
CHECKUP_TIMEOUT_SEC = 180


def _monthly_gate(db_path, key, day, hour, now):
    now = now or reminders.now_jst()
    if now.day != day or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, key)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 20:
                return False
        except ValueError:
            pass
    return True


# ---------------------------------------------------------- #65 組織図

def should_post_chart(db_path, agent_id, *, day=DAY_DEFAULT,
                      hour=CHART_HOUR, now=None):
    return _monthly_gate(db_path, CHART_STATE + agent_id, day, hour, now)


def mark_chart_posted(db_path, agent_id, message_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, CHART_STATE + agent_id,
                               last_checked_message_id=message_id,
                               last_run_at=reminders.fmt(now))


def previous_chart_id(db_path, agent_id):
    """前回の組織図メッセージid（編集で上書きするため）。"""
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, CHART_STATE + agent_id)
    return (state or {}).get("last_checked_message_id") or 0


def _first_line(profile):
    for ln in (profile or "").splitlines():
        ln = ln.strip().lstrip("-・ ").strip()
        if ln:
            return ln[:60]
    return ""


def collect_org(db_path, ai_members, now=None):
    """組織図の材料。humans は直近90日に発言があった人だけ（退職者を残さない）。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=90))
    humans = []
    with db.connect(db_path) as conn:
        for p in db.all_profiles(conn, limit=MAX_HUMANS * 2):
            row = conn.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE author_id=? AND deleted=0 AND created_at >= ?""",
                (p["user_id"], since.replace("T", " ")[:10])).fetchone()
            if row and row[0]:
                humans.append({"name": p["display_name"],
                               "role": _first_line(p["profile"]),
                               "recent": row[0]})
    humans.sort(key=lambda h: -h["recent"])
    return {"humans": humans[:MAX_HUMANS], "ai": ai_members}


def build_chart(org, updated_at):
    """組織図の本文（純粋関数）。推定であることを明記する。"""
    lines = ["🗂 **チーム組織図（自動更新）**", "", "**AIメンバー**"]
    for a in org["ai"]:
        extra = "（試用枠）" if a.get("trial") else ""
        lines.append(f"- {a['name']}{extra}: {a.get('role') or '全般'}")
    if org["humans"]:
        lines += ["", "**メンバー（直近90日に発言のある人・担当は会話からの推定）**"]
        for h in org["humans"]:
            role = f": {h['role']}" if h["role"] else ""
            lines.append(f"- {h['name']}{role}")
    lines.append("")
    lines.append(f"-# {updated_at} 時点の自動生成っス。"
                 "違ってたら教えてもらえれば直すっス（次回から反映されるっス）")
    return "\n".join(lines)


# ---------------------------------------------------------- #73 人格の定期健診

def should_checkup(db_path, agent_id, *, day=DAY_DEFAULT,
                   hour=CHECKUP_HOUR, now=None):
    return _monthly_gate(db_path, CHECKUP_STATE + agent_id, day, hour, now)


def mark_checkup(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, CHECKUP_STATE + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def sample_utterances(db_path, bot_user_id, limit=SAMPLE_MESSAGES):
    """そのエージェントの直近発言サンプル（健診の実測データ）。"""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT content FROM messages
               WHERE author_id=? AND deleted=0
                 AND content IS NOT NULL AND content != ''
               ORDER BY id DESC LIMIT ?""", (bot_user_id, limit)).fetchall()
    return [r[0][:200] for r in rows]


def build_checkup_prompt(name, persona_text, utterances):
    sample = "\n".join(f"- {u}" for u in utterances) or "（発言なし）"
    return (
        f"AIエージェント「{name}」の**人格定義**と**実際の発言**を突き合わせて、"
        "ズレがあれば指摘して。\n\n"
        f"【人格定義（宣言している姿）】\n{(persona_text or '')[:2000]}\n\n"
        f"【実際の直近発言】\n{sample}\n\n"
        "観点: 口調・一人称・語尾 / 距離感（敬語・砕け方）/ 長さ / "
        "担当領域からの逸脱。\n"
        "ルール: ズレが無ければ「概ね一致」とだけ書く。"
        "指摘は最大3件・各1行・具体例つき。修正案は書いてよいが、"
        "人格ファイルを書き換えるのは人間なので提案に留める。"
    )


def checkup(name, persona_text, utterances, *, model, invoke_fn=None):
    """健診を実行して所見テキストを返す（失敗時は None）。"""
    if not utterances:
        return None
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=CHECKUP_TIMEOUT_SEC).text)
    try:
        return (fn(build_checkup_prompt(name, persona_text,
                                        utterances)) or "").strip()[:800]
    except Exception:
        return None


def build_checkup_post(results):
    """全エージェント分の健診結果（純粋関数）。"""
    lines = ["🩺 人格の定期健診っス（宣言してる人格と実際の発言のズレ確認）"]
    for name, finding in results:
        lines.append(f"**{name}**")
        lines.append(finding or "（発言サンプルが足りず判定できなかったっス）")
    lines.append("-# 人格ファイルを直すかは管理者判断っス"
                 "（あたしたちは提案までっス）")
    return "\n".join(lines)
