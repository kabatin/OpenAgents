#!/usr/bin/env python3
"""予言の封筒（進化ロードマップ#94）。

月初に「今月こうなりそう」という予測をDBへ封印し、翌月に開封して答え合わせ
する。外れても実害の無い形で、予測能力が実務に使えるかを測る実験装置
（当初「予測はデータ不足で見送り」とした判断の、安全な入り口）。

単体テスト: ./venv/bin/python -m unittest test_meta_pack -v
"""

import json
import os
import re
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
DAY_DEFAULT = 1
HOUR_DEFAULT = 17
MAX_ITEMS = 3
TIMEOUT_SEC = 180
_JSON_RE = re.compile(r"\{.*\}", re.S)


def period_of(now):
    return now.strftime("%Y-%m")


def should_run(db_path, agent_id, *, day=DAY_DEFAULT, hour=HOUR_DEFAULT,
               now=None):
    now = now or reminders.now_jst()
    if now.day != day or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        return not db.prophecy_exists(conn, agent_id, period_of(now))


def build_seal_prompt(period, context_lines):
    listing = "\n".join(f"- {c}" for c in context_lines[:30]) or "（材料なし）"
    return (
        f"社内の記録をもとに、{period} に起きそうなことを予測して封印する"
        "（当たっても外れても実害はない実験っス）。\n\n"
        f"【最近の記録】\n{listing}\n\n"
        "ルール:\n"
        f"- 予測は最大{MAX_ITEMS}件。翌月に「当たった/外れた」を判定できる"
        "具体的な内容にする（例: 『Xの発注が今月中に完了する』）\n"
        "- 願望や当たり前のことは書かない（『忙しくなる』等は不可）\n"
        '- 出力はJSONのみ: {"predictions": [{"text": "…", '
        '"basis": "そう考える根拠1文"}]}'
    )


def parse_predictions(raw):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for p in (data.get("predictions") or [])[:MAX_ITEMS]:
        if isinstance(p, dict) and str(p.get("text") or "").strip():
            out.append({"text": str(p["text"]).strip()[:120],
                        "basis": str(p.get("basis") or "").strip()[:120]})
    return out


def collect_context(db_path, now=None):
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=45))
    out = []
    with db.connect(db_path) as conn:
        for (t,) in conn.execute(
                "SELECT decision FROM decisions WHERE created_at >= ?",
                (since,)).fetchall():
            out.append(t)
        for (t,) in conn.execute(
                """SELECT task FROM action_items
                   WHERE status='open' AND created_at >= ?""",
                (since,)).fetchall():
            out.append(t)
    return [t for t in out if (t or "").strip()]


def seal(db_path, agent_id, *, model, invoke_fn=None, now=None):
    """予測を封印する。封印した予測リスト（材料不足なら空）。"""
    now = now or reminders.now_jst()
    ctx = collect_context(db_path, now)
    if len(ctx) < 4:
        return []
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC).text)
    preds = parse_predictions(fn(build_seal_prompt(period_of(now), ctx)))
    if not preds:
        return []
    with db.connect(db_path) as conn:
        db.add_prophecy(conn, agent_id=agent_id, period=period_of(now),
                        payload_json=json.dumps(preds, ensure_ascii=False),
                        created_at=reminders.fmt(now))
    return preds


def build_open_prompt(period, preds, context_lines):
    listing = "\n".join(f"- {c}" for c in context_lines[:40]) or "（記録なし）"
    plist = "\n".join(f"{i}. {p['text']}" for i, p in enumerate(preds, 1))
    return (
        f"{period} の初めに封印した予測が当たったか、その後の記録で判定して。\n\n"
        f"【封印した予測】\n{plist}\n\n"
        f"【その後の記録】\n{listing}\n\n"
        "判定できない（記録に情報が無い）ものは hit=false・"
        "note に「判定材料なし」と書く。甘い判定をしない。\n"
        '出力はJSONのみ: {"verdicts": [{"index": 1, "hit": true, '
        '"note": "8/8に発注完了の記録あり"}]}'
    )


def parse_verdicts(raw, count):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for v in (data.get("verdicts") or [])[:count]:
        if not isinstance(v, dict):
            continue
        try:
            idx = int(v.get("index"))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= count:
            out.append({"index": idx, "hit": bool(v.get("hit")),
                        "note": str(v.get("note") or "")[:100]})
    return out


def open_envelope(db_path, agent_id, *, model, invoke_fn=None, now=None):
    """前の期の封筒を開けて答え合わせ。(period, preds, verdicts) or None。"""
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        env = db.sealed_prophecy_before(conn, agent_id, period_of(now))
    if env is None:
        return None
    preds = json.loads(env["payload_json"] or "[]")
    if not preds:
        return None
    ctx = collect_context(db_path, now)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC).text)
    verdicts = parse_verdicts(
        fn(build_open_prompt(env["period"], preds, ctx)), len(preds))
    with db.connect(db_path) as conn:
        db.open_prophecy(conn, env["id"],
                         json.dumps(verdicts, ensure_ascii=False),
                         reminders.fmt(now))
    return {"period": env["period"], "preds": preds, "verdicts": verdicts}


def build_post(sealed, opened, accuracy):
    lines = []
    if opened:
        hits = sum(1 for v in opened["verdicts"] if v["hit"])
        lines.append(f"📜 {opened['period']}の予言、開封するっス！"
                     f"（{hits}/{len(opened['preds'])} 的中）")
        by_idx = {v["index"]: v for v in opened["verdicts"]}
        for i, p in enumerate(opened["preds"], 1):
            v = by_idx.get(i)
            mark = "⭕" if (v and v["hit"]) else "❌"
            note = f"（{v['note']}）" if v and v["note"] else ""
            lines.append(f"{mark} {p['text']}{note}")
    if sealed:
        lines.append("✉️ 今月の予言を封印したっス（来月開封するっス）:")
        for p in sealed:
            lines.append(f"- {p['text']}")
    if accuracy and accuracy["total"]:
        pct = round(100 * accuracy["hit"] / accuracy["total"])
        lines.append(f"-# 通算的中率 {pct}%"
                     f"（{accuracy['hit']}/{accuracy['total']}）")
    return "\n".join(lines) if lines else None
