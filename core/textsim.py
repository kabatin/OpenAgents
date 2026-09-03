#!/usr/bin/env python3
"""短い日本語文の「言い直し」判定（純粋関数・依存なし）。

LLM抽出は同じ内容を語順や助詞違いで何度も出す（決定台帳で「同じ決定が6行」、
イベント逆算で「同じ開催地決定が3回」）。Jaccardだと片方に住所等の追記が
あるだけで落ちるので、文字2-gramの重なり率（共通数÷短い方）で判定する。

単体テスト: test_event_planner（is_same_event経由）・test_decisions
"""

import re

SAME_TEXT_OVERLAP = 0.6
MIN_BIGRAMS = 6   # これ未満の短文（「決定1」等）は重なり率が不安定なので照合しない
_STRIP_RE = re.compile(r"[\s（）()、。,.「」『』]")


def bigrams(text):
    t = _STRIP_RE.sub("", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def is_same_text(a, b, threshold=SAME_TEXT_OVERLAP):
    """a と b が同じ内容の言い直しか。短すぎる文は常に False（安全側）。"""
    ga, gb = bigrams(a), bigrams(b)
    if min(len(ga), len(gb)) < MIN_BIGRAMS:
        return False
    return len(ga & gb) / min(len(ga), len(gb)) >= threshold


def find_same(text, candidates, threshold=SAME_TEXT_OVERLAP):
    """candidates（文字列 or dict）から text の言い直しを1件返す（無ければ None）。
    dict の場合は key='decision' → 'name' → 'task' の順で本文を探す。"""
    for c in candidates:
        body = c if isinstance(c, str) else (
            c.get("decision") or c.get("name") or c.get("task") or "")
        if is_same_text(text, body, threshold):
            return c
    return None
