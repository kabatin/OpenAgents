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


def build_prompt(question, answer):
    """自己採点プロンプト（純粋関数）。5=問題なし〜1=問題あり。"""
    return (
        "社内AIアシスタントの回答を採点者として評価して。\n\n"
        f"【質問】\n{(question or '')[:500]}\n\n"
        f"【回答】\n{(answer or '')[:1500]}\n\n"
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


def review(question, answer, *, model, invoke_fn=None):
    """1回の自己採点。失敗は None（best-effort・本流に影響させない）。"""
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=REVIEW_TIMEOUT_SEC).text)
    try:
        return parse(fn(build_prompt(question, answer)))
    except Exception:
        return None
