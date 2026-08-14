#!/usr/bin/env python3
"""
サイコロ／抽選プラグイン（プラグインスキルの動作例・builder出力の見本）。

規約に沿った最小のプラグイン。bot.py を編集せず tools/ に置くだけで、
全エージェントが「NdM」形式のダイスを振れるようになる。
乱数は seed 固定不可の用途なので random を使う（テストは範囲で検証）。
"""

import random
import re

NAME = "dice"
MARKER = "DICE"
SKILL_NOTE = (
    "【ダイススキル】サイコロ・抽選・ランダムな決めごとを頼まれたら、"
    "返信本文の最後に改行して [DICE: NdM] を付ける"
    "（N個のM面ダイス。例: [DICE: 2d6]。1個なら 1d6）。"
    "遊び・くじ引き・順番決め等に使い、それ以外には付けないこと。"
)

_RE = re.compile(r"^(\d{1,2})d(\d{1,3})$", re.IGNORECASE)


def roll(n, m):
    """N個のM面ダイスを振り (合計, 各目) を返す（純粋ロジック・テスト対象）。"""
    if not (1 <= n <= 20 and 2 <= m <= 100):
        raise ValueError("ダイスは 1〜20個・2〜100面にしてください")
    eyes = [random.randint(1, m) for _ in range(n)]
    return sum(eyes), eyes


def handle(arg):
    """[DICE: 2d6] の引数を受け、結果テキストを返す。"""
    mm = _RE.match((arg or "").strip())
    if not mm:
        return "🎲 ダイスの形式は NdM（例: 2d6）で指定してください"
    total, eyes = roll(int(mm.group(1)), int(mm.group(2)))
    detail = "+".join(str(e) for e in eyes)
    if len(eyes) == 1:
        return f"🎲 {arg} → **{total}**"
    return f"🎲 {arg} → {detail} = **{total}**"
