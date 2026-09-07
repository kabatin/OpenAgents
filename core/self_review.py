#!/usr/bin/env python3
"""投稿前セルフレビュー（進化ロードマップ#14）— まずはシャドー計測。

回答の投稿「後」に安いモデルが自己採点し、proactive_log に記録するだけ
（投稿には触れない・レイテンシに乗せない）。スコア分布が溜まったら
「低スコア時の書き直し」を発動するかを判断する（シャドーモード規約）。

単体テスト: ./venv/bin/python -m unittest test_self_review -v
"""

import json
import os
import re

from core import invoke_claude
REVIEW_TIMEOUT_SEC = 60
MIN_ANSWER_LEN = 100   # 短い相槌まで採点しない（コスト・ノイズ防止）
_JSON_RE = re.compile(r"\{.*\}", re.S)


def build_prompt(question, answer, evidence=None):
    """自己採点プロンプト（純粋関数）。5=問題なし〜1=問題あり。
    evidence: 回答の途中で実際に使ったツールと結果の要約（v4 Phase 4）。
    これが無いと「一覧を検索して答えた人数」まで「根拠のない断定」と誤採点する。"""
    ev = ""
    if evidence:
        ev = (f"【根拠（回答の途中で実際に使ったツールと結果）】\n{evidence[:1200]}\n\n"
              "上のツールで確認した事実に基づく言い切りは「根拠のない断定」と"
              "みなさない。逆に、チーム固有の事実をツールを使わずに断定していれば"
              "減点する。\n\n")
    return (
        "社内AIアシスタントの回答を採点者として評価して。\n\n"
        f"【質問】\n{(question or '')[:500]}\n\n"
        f"【回答】\n{(answer or '')[:1500]}\n\n"
        + ev +
        "観点: 事実の危うさ（根拠のない断定）/ 質問への的中 / 長さの適切さ / "
        "トーン。\n"
        "出力はJSONのみ: {\"score\": 1-5, \"issue\": \"一番の問題を一言"
        "（5点なら空文字）\"}"
    )


def parse(raw):
    """採点JSONの解釈（純粋関数・壊れていたら None＝記録しない）。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        score = int(data.get("score"))
    except (ValueError, TypeError):
        return None
    if not 1 <= score <= 5:
        return None
    return {"score": score, "issue": str(data.get("issue") or "")[:100]}


def review(question, answer, *, model, invoke_fn=None, evidence=None):
    """1回の自己採点。失敗は None（best-effort・本流に影響させない）。"""
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=REVIEW_TIMEOUT_SEC,
        purpose="self_review").text)
    try:
        return parse(fn(build_prompt(question, answer, evidence=evidence)))
    except Exception:
        return None
