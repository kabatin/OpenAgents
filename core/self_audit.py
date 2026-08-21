#!/usr/bin/env python3
"""自己監査日誌（RM#84）＋バイアス点検（RM#86）。

#84: 毎晩、その日の「怪しかった判断」（裏取り失敗の断定・できたフリ検出・
     懐疑役の差し止め・セルフレビュー低評価）を1本に集約してホームchへ。
     材料は全部 proactive_log に既にあるので集計は決定論。
#86: 月次で「誰に・どのチャンネルに偏って反応しているか」を集計して報告。
     特定の人にだけ応答が集中していないかを数字で見せる（claude不使用）。

単体テスト: ./venv/bin/python -m unittest test_meta_pack -v
"""

import os
from datetime import timedelta


from core import db
from core import reminders
AUDIT_STATE = "audit:"
BIAS_STATE = "bias:"
AUDIT_HOUR = 23           # 夜の総括
BIAS_DAY = 1
BIAS_HOUR = 16
BIAS_MIN_TOTAL = 8        # これ未満の発言数では偏りを語らない
BIAS_DOMINANT = 0.6       # 1人/1chが6割超なら偏りとして指摘

LABELS = {"assert_flagged": "裏取りできない断定",
          "caught": "できたフリ検出", "score": "セルフレビュー低評価",
          "silent": "懐疑役が差し止め"}


def should_audit(db_path, agent_id, *, hour=AUDIT_HOUR, now=None):
    now = now or reminders.now_jst()
    if now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, AUDIT_STATE + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).total_seconds() < 20 * 3600:
                return False
        except ValueError:
            pass
    return True


def mark_audited(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, AUDIT_STATE + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def collect_audit(db_path, agent_id, now=None):
    """当日の怪しい判断（低評価は3点以下のみ拾う）。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now.replace(hour=0, minute=0))
    with db.connect(db_path) as conn:
        rows = db.audit_items_since(conn, agent_id, since)
    out = []
    for r in rows:
        if r["action"] == "score":
            head = (r["detail"] or "0")[:1]
            if not head.isdigit() or int(head) > 3:
                continue      # 4〜5点は問題なし
        out.append(r)
    return out


def category_counts(items):
    """カテゴリ別の件数（ログ用の内訳・純粋関数）。"""
    counts = {}
    for r in items:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return counts


def format_counts(counts):
    """ログ・ダッシュボード用の内訳文字列（純粋関数）。"""
    return "・".join(f"{LABELS.get(k, k)}{v}件"
                     for k, v in sorted(counts.items())) or "0件"


def build_audit_post(items):
    """夜の自己監査（純粋関数）。何も無ければ None＝投稿しない。"""
    if not items:
        return None
    lines = ["🌙 今日の自己監査っス（あやしかった判断の振り返り）"]
    for r in items[:8]:
        label = LABELS.get(r["action"], r["action"])
        detail = (r["detail"] or "").replace("\n", " ")[:70]
        lines.append(f"- [{label}] {detail}")
    if len(items) > 8:
        lines.append(f"-# 他{len(items) - 8}件")
    lines.append("-# 明日はここを気をつけるっス。"
                 "見当違いだったら教えてほしいっス🙏")
    return "\n".join(lines)


# ------------------------------------------------------ #86 バイアス点検

def should_check_bias(db_path, agent_id, *, day=BIAS_DAY, hour=BIAS_HOUR,
                      now=None):
    now = now or reminders.now_jst()
    if now.day != day or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, BIAS_STATE + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 20:
                return False
        except ValueError:
            pass
    return True


def mark_bias_checked(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, BIAS_STATE + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def collect_bias(db_path, agent_id, now=None, days=30):
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=days))
    with db.connect(db_path) as conn:
        stats = db.bias_stats(conn, agent_id, since)
        # 救済記録（2026-08-18で接続）: 誰の質問が放置されがちかも同じ点検で見る
        stats["rescues"] = db.rescue_stats(conn, agent_id, since)
    return stats


def analyze_bias(stats):
    """偏りの判定（純粋関数）。総数が少なければ None（語らない）。"""
    people, channels = stats.get("people") or [], stats.get("channels") or []
    total = sum(p["n"] for p in people)
    if total < BIAS_MIN_TOTAL:
        return None
    findings = []
    if people and people[0]["n"] / total >= BIAS_DOMINANT:
        findings.append(f"反応の{round(100 * people[0]['n'] / total)}%が"
                        f"{people[0]['name']}さん宛に集中")
    if channels and channels[0]["n"] / total >= BIAS_DOMINANT:
        findings.append(f"発言の{round(100 * channels[0]['n'] / total)}%が"
                        f"#{channels[0]['name']}に集中")
    # 救済の偏り（誰の質問が沈みやすいか）＝組織側の弱点として併記する
    rescues = stats.get("rescues") or {}
    r_people = rescues.get("people") or []
    r_total = rescues.get("total") or 0
    if r_total >= BIAS_MIN_TOTAL and r_people \
            and r_people[0]["n"] / r_total >= BIAS_DOMINANT:
        findings.append(
            f"放置されがちな質問の{round(100 * r_people[0]['n'] / r_total)}%が"
            f"{r_people[0]['name']}さんの投稿（拾い上げ{r_total}件中）")
    return {"total": total, "people": people[:5], "channels": channels[:5],
            "findings": findings, "rescues": {"total": r_total,
                                              "people": r_people[:3]}}


def build_bias_post(agent_name, result):
    if result is None:
        return None
    lines = [f"⚖️ {agent_name}の反応バイアス点検（直近30日・{result['total']}件）"]
    lines.append("- 宛先: " + "、".join(
        f"{p['name']}{p['n']}件" for p in result["people"]) or "—")
    lines.append("- 場所: " + "、".join(
        f"#{c['name']}{c['n']}件" for c in result["channels"]) or "—")
    rescues = result.get("rescues") or {}
    if rescues.get("total"):
        who = "、".join(f"{p['name']}{p['n']}件"
                        for p in rescues.get("people") or [])
        lines.append(f"- 拾い上げた未回答質問: {rescues['total']}件（{who}）")
    if result["findings"]:
        lines.append("⚠️ " + "／".join(result["findings"])
                     + "。他の人・他のchも見るようにするっス")
    else:
        lines.append("-# 大きな偏りは無さそうっス")
    return "\n".join(lines)
