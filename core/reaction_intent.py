#!/usr/bin/env python3
"""リアクションの意味の切り分け。

「✅=登録 / ❌=見送り」のように操作を頼んだ投稿へのリアクションは、承認・却下の
操作であって、発言の良し悪しの評価ではない。これを👍👎の物差し（feedback・
勝ちパターン・教訓・ゴールデン）に混ぜると、見送っただけで「嫌われた」と学習する
（切り出し元の実データでは👎31件のうち少なくとも12件がこの混入だった）。

判定は投稿本文に「絵文字＋操作の指示」があるかどうか（決定論・純粋関数）。
提案を出す箇所が増えても、本文に押し方を書けば自動で対象になる。
単体テスト: test_reaction_intent.py
"""

import re

_EMOJI = "[👍👎✅❌🙅]"
_APPROVAL_HINT = re.compile(
    rf"{_EMOJI}\s*(?:[=＝:：→]|で|なら|を押|を付|押し)"   # ✅=登録 / ❌で見送り
    rf"|たら\s*{_EMOJI}")                               # 済んでいたら👍


def is_approval_prompt(text):
    """リアクションで操作を頼んでいる投稿か。"""
    return bool(text) and _APPROVAL_HINT.search(text) is not None
