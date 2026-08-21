#!/usr/bin/env python3
"""振り返りの週次蒸留 — 記録するだけだった自己点検を消費するループ。

夜の自己監査日誌（RM#84）は毎晩「あやしかった判断」を4カテゴリで見せていたが、
報告して終わりで翌日の振る舞いに反映される道が無かった（2026-08-18の指摘）。
そこで週1回、次の4カテゴリを横断で集めてLLMが蒸留し、最大3つの短い改善助言に
して教訓帳（proactive_lessons / polarity='advice'）へ**差し替え**で保存する。
助言は通常回答のプロンプトに常時注入される（bot.py）。

  - selfreview : 投稿後セルフレビューの低スコア（3点以下）の問題点
  - assertion  : 裏取りできない断定（honesty のシャドー計測）
  - fake_done  : できたフリ検出に捕まった回答
  - skeptic    : 懐疑役が投稿を差し止めた自発発言

- 静かな処理: Discordへの投稿はしない。実行の痕跡は proactive_log
  （kind='selfreview_distill'）に残り、ダッシュボードのタイムラインで見える
- 差し替え式: 最新の蒸留だけが生きる＝古い助言が無限に積もらない
- データが薄い週（低スコアがMIN_SAMPLES未満）は何もしない（ノイズ防止）

単体テスト: ./venv/bin/python -m unittest test_growth_pack -v
"""

import json
import os
import re
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
STATE_KEY_PREFIX = "svdistill:"
WEEKDAY_DEFAULT = 0    # 月曜（rule_distillの1時間前）
HOUR_DEFAULT = 9
WINDOW_DAYS = 14       # 直近2週間ぶんを見る（週次実行の取りこぼし吸収）
LOW_SCORE_MAX = 2      # このスコア以下を「低スコア」とする
MIN_SAMPLES = 3        # 低スコアがこれ未満の週は蒸留しない
MAX_ADVICE = 3
MAX_ADVICE_LEN = 60
DISTILL_TIMEOUT_SEC = 180
_JSON_RE = re.compile(r"\[.*\]", re.S)


def should_run(db_path, agent_id, *, weekday=WEEKDAY_DEFAULT,
               hour=HOUR_DEFAULT, now=None):
    """週1ガード（rule_distill.should_send と同じ流儀）。"""
    now = now or reminders.now_jst()
    if now.weekday() != weekday or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_KEY_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 3:
                return False
        except ValueError:
            pass
    return True


def mark_ran(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_KEY_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def parse_score_detail(detail):
    """proactive_log の detail（'スコア|問題点'）を解釈（純粋関数）。"""
    if not detail or "|" not in detail:
        return None
    head, issue = detail.split("|", 1)
    try:
        score = int(head.strip())
    except ValueError:
        return None
    if not 1 <= score <= 5:
        return None
    return {"score": score, "issue": issue.strip()}


CATEGORY_LABELS = {
    "selfreview": "低評価だった回答の問題点",
    "assertion": "裏取りできないまま断定した箇所",
    "fake_done": "やったと言ったが実行されていなかった件",
    "skeptic": "懐疑役が投稿を差し止めた理由",
}


def summarize_detail(category, detail):
    """カテゴリ別に蒸留へ渡す1行を作る（純粋関数・対象外なら None）。"""
    detail = (detail or "").strip().replace("\n", " ")
    if not detail:
        return None
    if category == "selfreview":
        parsed = parse_score_detail(detail)
        if not parsed or parsed["score"] > LOW_SCORE_MAX \
                or not parsed["issue"]:
            return None      # 高評価は改善材料にしない
        return parsed["issue"][:120]
    if category == "skeptic":
        # 「懐疑役が差し止め: 〜」の理由部分だけを取る
        _, _, reason = detail.partition(":")
        return (reason.strip() or detail)[:120]
    return detail[:120]


def collect_low_issues(db_path, agent_id, now=None):
    """観察窓内の振り返り材料（4カテゴリ横断・新しい順）。
    Returns: [(カテゴリ, 1行)]"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=WINDOW_DAYS))
    with db.connect(db_path) as conn:
        rows = db.reflection_items_since(conn, agent_id, since)
    issues = []
    for r in rows:
        line = summarize_detail(r["category"], r["detail"])
        if line:
            issues.append((r["category"], line))
    return issues


def build_prompt(issues):
    """蒸留プロンプト（純粋関数）。カテゴリごとにまとめて渡す。"""
    grouped = {}
    for category, line in issues[:60]:
        grouped.setdefault(category, []).append(line)
    blocks = []
    for category, lines in grouped.items():
        label = CATEGORY_LABELS.get(category, category)
        body = "\n".join(f"- {ln}" for ln in lines[:20])
        blocks.append(f"【{label}】\n{body}")
    return (
        "社内AIアシスタントが自分の応答を点検して見つけた問題の一覧です"
        "（種類ごとにまとめてあります）。\n\n"
        + "\n\n".join(blocks) + "\n\n"
        "繰り返し現れる改善因子を最大3つに蒸留してください。各項目は"
        f"{MAX_ADVICE_LEN}文字以内・「〜する」の命令形の短文で、次回の回答時に"
        "そのまま心がけられる形にすること。一度きりの個別事象は含めない。"
        "複数の種類にまたがる共通の癖があれば優先して挙げる。\n"
        '出力はJSON配列のみ: ["助言1", "助言2"]'
    )


def parse(raw):
    """蒸留結果の解釈（純粋関数・壊れていたら空リスト＝何もしない）。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:MAX_ADVICE]:
        text = str(it).strip()[:MAX_ADVICE_LEN]
        if text:
            out.append(text)
    return out


def distill(db_path, agent_id, *, model, invoke_fn=None, now=None):
    """1回の蒸留。保存した助言リストを返す（データ不足なら空リスト）。"""
    issues = collect_low_issues(db_path, agent_id, now=now)
    if len(issues) < MIN_SAMPLES:
        return []
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=DISTILL_TIMEOUT_SEC).text)
    advice = parse(fn(build_prompt(issues)))
    if not advice:
        return []
    with db.connect(db_path) as conn:
        db.replace_advice_lessons(conn, agent_id, advice,
                                  reminders.fmt(now or reminders.now_jst()))
    return advice


def build_advice_block(advice_rows):
    """通常回答プロンプト用の助言ブロック（純粋関数・無ければ空文字）。"""
    if not advice_rows:
        return ""
    lines = [f"- {r['text']}" for r in advice_rows]
    return ("【自己改善メモ（自分の点検記録から自動蒸留した心がけ）】\n"
            + "\n".join(lines))
