#!/usr/bin/env python3
"""
スレッド返信（能力起票 #12）の純粋ロジック。

「チャンネルへ直接投稿」ではなく「対象の投稿からスレッドを開始してその中へ
返信」できるようにするための、設定の正規化・発火判定・スレッド名の生成だけを
担う副作用の無いモジュール。実際のスレッド作成・投稿（IO）は agent_runtime の
deliver_reply が行い、ここはそこから薄く呼ばれる（純粋関数とIOの分離）。

外向きの挙動変更なので config フラグ（既定オフ）＋シャドー（既定オン）で守る。
agent 設定に次を足すと有効になる（キー自体が無ければ全既定＝オフのまま）:
  "thread_reply": {
    "enabled": true,             # 既定 false（キー無し＝オフ）
    "shadow": true,              # 既定 true（実スレッドは作らず記録だけ）
    "flows": ["mention", "pdf"]  # 対象フロー（既定は両方）
  }
本投稿（実際にスレッドへ返す）は shadow を false にして初めて解禁される。

単体テスト: ./venv/bin/python -m unittest test_thread_reply -v
"""

import re

# Discordのスレッド名は1〜100文字。生成名は必ずこの範囲へ収める。
THREAD_NAME_MAX = 100
# 対象フロー: "mention"=メンション/ホーム応答、"pdf"=PDF自動要約。
DEFAULT_FLOWS = ("mention", "pdf")
# 本文が空（PDFのみ投稿等）のときのフロー別スレッド名。
_KIND_DEFAULTS = {"mention": "スレッド返信", "pdf": "PDF要約"}
_WS_RE = re.compile(r"\s+")


def normalize(raw):
    """agent設定の thread_reply を正規化する。None/キー無しは全既定（オフ）。
    enabled 既定 False・shadow 既定 True・flows 既定 DEFAULT_FLOWS。"""
    raw = raw or {}
    flows = raw.get("flows") or DEFAULT_FLOWS
    return {
        "enabled": bool(raw.get("enabled")),
        "shadow": bool(raw.get("shadow", True)),
        "flows": tuple(flows),
    }


def wants(cfg, kind, *, is_bot=False):
    """このフローでスレッドを開くべきか。Bot発言・無効・対象外フローは常にFalse
    （Bot同士の会話でスレッドを量産しないための決定的ガード）。"""
    if is_bot or not cfg.get("enabled"):
        return False
    return kind in (cfg.get("flows") or ())


def thread_name(text, *, kind):
    """対象投稿の本文からスレッド名を作る（空白畳み込み＋100字上限）。
    本文が無ければフロー別の既定名。必ず1〜100文字の非空文字列を返す。"""
    base = _WS_RE.sub(" ", text or "").strip()
    if not base:
        base = _KIND_DEFAULTS.get(kind, "スレッド")
    if len(base) > THREAD_NAME_MAX:
        base = base[:THREAD_NAME_MAX - 1].rstrip() + "…"
    return base
