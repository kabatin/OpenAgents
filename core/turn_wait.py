#!/usr/bin/env python3
"""言い終わってから答える（連投・入力中を待ってまとめて1回答える）。

Discord では短い投稿を連投する人や、打つのに時間がかかる人がいる。1通目だけで
答え始めると話の半分にしか答えられない。そこで、
- 依頼が言い終わっているか（言い終わり / 言いかけ / どちらとも言えない）で待つ時間を変える
- その人が入力中なら待ちを延ばす（入力中の表示）
- 待っている間の同じ人の投稿は本文をつなげて、まとめて1回だけ答える
判定と待ち時間はここ（純粋関数・プラットフォーム非依存）、投稿の束ね方と
入力中の受け取りは platforms/discord/bot.py。

単体テスト: core/test_turn_wait.py
"""

import re

QUIET_SEC = {"done": 2, "unclear": 8, "partial": 60}
TYPING_GRACE_SEC = 12      # 最後の「入力中」からこの秒数は、まだ打っているとみなす
MAX_WAIT_SEC = 180
MAX_WAIT_CAP = 600

_DONE = re.compile(
    r"([?？。！!]|して|ください|下さい|お願い(します|いたします|です)?|教えて|"
    r"頼む|頼みます|かな|ほしい|欲しい|ますか|ですか|だっけ|かも)\s*$")
_PARTIAL = re.compile(
    r"([、,，:：…]|\.\.\.|けど|けれど|けども|ので|から|が|で|に|って|とか|"
    r"あの|えーと|えっと|ちなみに)\s*$")
SHORT_LEN = 6


def completeness(text):
    """'done'（言い終わり）/ 'partial'（言いかけ）/ 'unclear'。"""
    t = (text or "").strip()
    if not t:
        return "partial"
    if _DONE.search(t):
        return "done"
    if _PARTIAL.search(t) or len(t) <= SHORT_LEN:
        return "partial"
    return "unclear"


def should_fire(now, *, first_at, last_msg_at, last_typing_at, kind,
                max_total=MAX_WAIT_SEC):
    """いま答え始めてよいか（時刻は秒）。上限を超えたら必ず動く。"""
    if now - first_at >= max_total:
        return True
    if last_typing_at is not None and now - last_typing_at < TYPING_GRACE_SEC:
        return False
    return now - last_msg_at >= QUIET_SEC.get(kind, QUIET_SEC["unclear"])


def merge(texts):
    """連投をつなげた質問文（空の投稿は落とす）。"""
    return "\n".join(t.strip() for t in texts if t and t.strip())


def normalize(cfg):
    """エージェント設定 turn_wait を {enabled, max_wait_sec} にする（既定オフ）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        cap = int(cfg.get("max_wait_sec", MAX_WAIT_SEC))
    except (TypeError, ValueError):
        cap = MAX_WAIT_SEC
    return {"enabled": cfg.get("enabled") is True,
            "max_wait_sec": max(10, min(cap, MAX_WAIT_CAP))}
