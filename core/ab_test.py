#!/usr/bin/env python3
"""A/Bプロンプト実験（進化ロードマップ#12）。

同じ役割のプロンプト（いまは自発発言の一次判定＝screen）に複数の変種を登録し、
使うたびにラウンドロビンで選んで使用回数と👍👎を貯める。十分なサンプルが
溜まって明確な差がついたときだけ「採用提案」を出す（**自動採用はしない**＝
物差しと置換の判断は人間が持つ、の原則を守る）。

単体テスト: ./venv/bin/python -m unittest test_batch_pack3 -v
"""

import os
import re


from core import db
from core import reminders
SLOT_SCREEN = "screen_note"
MIN_SAMPLES = 12       # 各変種これだけ使われるまで比較しない
MIN_GAP = 0.25         # 👍率の差がこれ以上でないと提案しない

# 変種は人間が書いた固定文（LLMに自己書き換えさせない＝安全弁）
DEFAULT_VARIANTS = {
    "A": "",   # 現行のまま（対照群）
    "B": ("- 一言添えるなら、相手の作業を止めない短さにする"
          "（1〜2文で足りるなら候補にする）"),
}


def ensure_variants(db_path, slot=SLOT_SCREEN, variants=None):
    """変種を登録（冪等）。"""
    variants = variants or DEFAULT_VARIANTS
    now = reminders.fmt(reminders.now_jst())
    with db.connect(db_path) as conn:
        for name, body in variants.items():
            db.upsert_prompt_variant(conn, slot=slot, variant=name,
                                     body=body, created_at=now)


def pick_for_screen(db_path):
    """一次判定に使う変種を選ぶ（初回は自動で種撒きする）。
    Returns: (variant_id, 追記文) or (None, "")。
    2026-08-18: ensure_variants の呼び出しが配線されておらず、変種ゼロのまま
    評価が空回りしていた。使う側で必ず種を撒くことで起動漏れを構造的に防ぐ。"""
    ensure_variants(db_path)
    got = pick(db_path)
    return got if got else (None, "")


def pick(db_path, slot=SLOT_SCREEN):
    """使用回数が最少の変種を選ぶ（ラウンドロビン）。(id, body) or None。"""
    with db.connect(db_path) as conn:
        rows = db.active_variants(conn, slot)
        if not rows:
            return None
        chosen = min(rows, key=lambda r: (r["used"], r["id"]))
        db.bump_variant_used(conn, chosen["id"])
    return chosen["id"], chosen["body"]


def record_feedback(db_path, variant_id, value):
    """その変種で生成した発言への👍👎を記録。"""
    with db.connect(db_path) as conn:
        db.bump_variant_feedback(conn, variant_id, value)


VARIANT_TAG_RE = re.compile(r"variant=(\d+)")


def tag(variant_id):
    """proactive_log の detail に埋める帰属タグ（純粋関数）。"""
    return f"variant={variant_id}" if variant_id else ""


def variant_from_detail(detail):
    """detail から変種idを取り出す（純粋関数・無ければ None）。"""
    m = VARIANT_TAG_RE.search(detail or "")
    return int(m.group(1)) if m else None


def record_feedback_for_message(db_path, message_id, value):
    """自発発言への👍👎を、その発言を生んだ変種へ帰属させる（RM#12）。
    Returns: 帰属した変種id or None。"""
    with db.connect(db_path) as conn:
        detail = db.proactive_log_detail(conn, message_id)
    vid = variant_from_detail(detail)
    if vid is None:
        return None
    record_feedback(db_path, vid, value)
    return vid


def evaluate(db_path, slot=SLOT_SCREEN):
    """比較結果（純粋な集計）。提案できる差があれば dict、無ければ None。"""
    with db.connect(db_path) as conn:
        rows = db.active_variants(conn, slot)
    scored = []
    for r in rows:
        n = r["up"] + r["down"]
        if r["used"] < MIN_SAMPLES:
            return None            # サンプル不足の変種があるうちは比較しない
        rate = (r["up"] / n) if n else 0.0
        scored.append({**r, "rate": rate, "judged": n})
    if len(scored) < 2:
        return None
    scored.sort(key=lambda x: -x["rate"])
    best, worst = scored[0], scored[-1]
    if best["rate"] - worst["rate"] < MIN_GAP:
        return None
    return {"best": best, "worst": worst}


def build_report(result, slot=SLOT_SCREEN):
    b, w = result["best"], result["worst"]
    return (f"🧪 プロンプトA/B（{slot}）で差が出たっス:\n"
            f"- 変種{b['variant']}: 👍率{round(b['rate'] * 100)}%"
            f"（{b['up']}/{b['judged']}件・使用{b['used']}回）\n"
            f"- 変種{w['variant']}: 👍率{round(w['rate'] * 100)}%"
            f"（{w['up']}/{w['judged']}件・使用{w['used']}回）\n"
            f"-# 変種{b['variant']}に寄せた方が良さそうっス。"
            "採用するかは管理者判断でお願いするっス"
            "（自動では切り替えないっス）")
