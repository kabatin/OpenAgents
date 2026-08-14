#!/usr/bin/env python3
"""外部連携（インテグレーション）の登録機構。

`plugins.py` との使い分け:
    plugins  = LLMが自分で書ける小さな道具。AST検査で os/subprocess/socket を
               禁じているため、**ネットワークを叩く連携は書けない**。
    ここ     = 人間が書く外部サービス連携（スプレッドシート・社内API・メール等）。
               任意のimportができる代わりに、config で明示的に有効化した
               ものだけを読み込む。

置き場所:
    <リポジトリルート>/integrations/<name>/__init__.py

有効化:
    config.json の `integrations.enabled: ["example_dice", "my_sheets"]`
    さらにエージェント個別に効かせたい連携は、エージェントの
    `skills.<SKILL_KEY>` を true にする（SKILL_KEY 未定義なら NAME を使う）。

連携モジュールが定義できるもの（すべて任意。あるものだけ呼ばれる）:

    NAME       = "my_sheets"            # 必須。一意な識別子
    SKILL_KEY  = "sheets"               # 任意。agent.skills のキー（既定 NAME）
    SUMMARY    = "…"                    # 任意。ダッシュボード表示用の1行説明

    def available(config) -> bool
        グローバル設定として使える状態か（APIキー未設定なら False 等）。
        未定義なら常に True。

    def skill_note(ctx) -> str | None
        システムプロンプトに足す「この道具の使い方」。回答生成の前に呼ばれる。

    def context_block(ctx, gate_text) -> str | None
        回答の材料になる現況スナップショット。**別スレッドで実行される**ので
        ネットワークI/Oを書いてよい。gate_text は「今の発言＋直近の会話」で、
        関係なさそうなら None を返して呼び出しを節約する。

    def apply_markers(ctx, answer) -> tuple[str, list[str]]
        回答本文に含まれる自前マーカー（例 `[SHEET_WRITE: ...]`）を実行し、
        (マーカーを除去した本文, 人間に見せる注記のリスト) を返す。
        **別スレッドで実行される**。

    CYCLES = [(name, fn), ...]
        観察ループに登録する定期処理。fn(ctx) を順に回す。

    PREHOOKS = [(name, fn), ...]
        呼ばれなくても発動する決定的トリガー。fn(ctx) が True を返すと
        「このターンは処理済み」として通常の応答生成を行わない。

単体テスト: python -m unittest test_integrations -v
"""

import importlib
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from core import paths

#: 連携パッケージの置き場（リポジトリルート/integrations）
INTEGRATIONS_DIR = paths.INTEGRATIONS_DIR


@dataclass(frozen=True)
class Context:
    """連携に渡す実行文脈。プラットフォーム非依存の値だけを入れる。

    Discord固有のオブジェクト（discord.Message 等）は**絶対に入れない**。
    Slack等に載せ替えたときに連携が壊れるため。
    """

    agent_id: str
    agent_name: str
    db_path: str
    config: dict = field(default_factory=dict)
    #: 発言者（プラットフォーム上のID・文字列）
    author_id: str = ""
    #: 発言者が管理者か
    is_admin: bool = False
    #: 発言のあったチャンネル
    channel_id: str = ""
    #: 発言そのもののID
    message_id: str = ""
    #: 添付ファイルのローカルパス（このターン限り。無ければ空）
    attachments: tuple = ()
    #: 連携が自由に使ってよい追加情報
    extra: dict = field(default_factory=dict)


class Integration:
    """読み込み済みの連携1件。未定義のフックは何もしない実装で埋める。"""

    def __init__(self, module: Any):
        self.module = module
        self.name = getattr(module, "NAME", None) or module.__name__
        self.skill_key = getattr(module, "SKILL_KEY", None) or self.name
        self.summary = getattr(module, "SUMMARY", "") or ""

    def available(self, config: dict) -> bool:
        fn = getattr(self.module, "available", None)
        if fn is None:
            return True
        try:
            return bool(fn(config))
        except Exception as exc:  # 連携の不具合でBOTを落とさない
            print(f"integration {self.name}: available() 失敗: {exc}")
            return False

    def _call(self, hook: str, *args, default=None):
        fn = getattr(self.module, hook, None)
        if fn is None:
            return default
        try:
            return fn(*args)
        except Exception as exc:
            print(f"integration {self.name}: {hook}() 失敗: {exc}")
            return default

    def skill_note(self, ctx: Context):
        return self._call("skill_note", ctx)

    def context_block(self, ctx: Context, gate_text: str):
        return self._call("context_block", ctx, gate_text)

    def apply_markers(self, ctx: Context, answer: str):
        got = self._call("apply_markers", ctx, answer)
        if not got:
            return answer, []
        try:
            text, notes = got
        except (TypeError, ValueError):
            print(f"integration {self.name}: apply_markers の戻り値が不正です")
            return answer, []
        return (text if isinstance(text, str) else answer), list(notes or [])

    @property
    def cycles(self) -> list:
        return list(getattr(self.module, "CYCLES", []) or [])

    @property
    def prehooks(self) -> list:
        return list(getattr(self.module, "PREHOOKS", []) or [])


def _enabled_names(config: dict) -> list:
    section = config.get("integrations") or {}
    names = section.get("enabled") or []
    return [str(n) for n in names if str(n).strip()]


def load(config: dict, base_dir: str | None = None) -> list:
    """config で有効化された連携だけを読み込む。

    読み込みに失敗したものは警告を出して**飛ばす**（1件の不具合で
    BOT全体を起動不能にしない）。戻り値は Integration のリスト。
    """
    base = base_dir or INTEGRATIONS_DIR
    if not os.path.isdir(base):
        return []
    if base not in sys.path:
        sys.path.insert(0, base)
    loaded = []
    seen = set()
    for name in _enabled_names(config):
        if name in seen:
            continue
        seen.add(name)
        if not os.path.isdir(os.path.join(base, name)):
            print(f"integration {name}: {base} に見つかりません（無視します）")
            continue
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"integration {name}: 読み込み失敗のため無効化します: {exc}")
            continue
        integration = Integration(module)
        if not integration.available(config):
            print(f"integration {name}: 設定が未完了のため無効です")
            continue
        loaded.append(integration)
    return loaded


def for_agent(integrations: list, agent: dict) -> list:
    """このエージェントで有効な連携だけに絞る。

    `agent.skills.<skill_key>` が真のものだけ。skills 自体が無ければ空。
    """
    skills = agent.get("skills") or {}
    return [i for i in integrations if skills.get(i.skill_key)]


def skill_notes(integrations: list, ctx: Context) -> list:
    """全連携の skill_note を集める（None・空文字は落とす）。"""
    notes = []
    for i in integrations:
        note = i.skill_note(ctx)
        if note:
            notes.append(note)
    return notes


def context_blocks(integrations: list, ctx: Context, gate_text: str) -> list:
    """全連携の context_block を集める。**別スレッドから呼ぶこと**。"""
    blocks = []
    for i in integrations:
        block = i.context_block(ctx, gate_text)
        if block:
            blocks.append(block)
    return blocks


def apply_all_markers(integrations: list, ctx: Context, answer: str):
    """全連携のマーカーを順に適用する。**別スレッドから呼ぶこと**。

    戻り値: (処理後の本文, 人間に見せる注記のリスト)
    """
    notes = []
    for i in integrations:
        answer, got = i.apply_markers(ctx, answer)
        notes.extend(got)
    return answer, notes
