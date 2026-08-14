#!/usr/bin/env python3
"""共有メモ帳の連携サンプル。

**これは動く見本です。** 外部サービスの代わりにローカルのJSONファイルを
読み書きするので、APIキーもネットワークも要らずにそのまま試せます。
本物の連携（スプレッドシート・社内API・チケット管理など）を書くときは、
このファイルをコピーして `_load` / `_save` の中身を差し替えてください。

使い方:
  1. config.json の `integrations.enabled` に "example_notes" を足す
  2. エージェントの `skills.notes` を true にする
  3. 再起動して「メモしといて: 来週の会議は水曜」と話しかける

連携が提供できるフック（chatbot/integrations.py 参照）:
  skill_note     … AIに「この道具の使い方」を教える
  context_block  … 回答の材料になる現況を渡す（別スレッドで実行）
  apply_markers  … AIが出したマーカーを実行する（別スレッドで実行）
  CYCLES         … 定期処理
  PREHOOKS       … 呼ばれなくても発動する決定的トリガー
"""

import json
import os
import re

NAME = "example_notes"
#: エージェントの skills.<これ> で有効化する（NAME と別名にできる）
SKILL_KEY = "notes"
SUMMARY = "共有メモ帳（連携の書き方サンプル）"

#: 保存先。実行時に作られるので gitignore 対象（state/ 配下）
_HERE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "..", "state", "example_notes.json"))

#: AIが書くマーカー。[NOTE_ADD: 本文] の形
MARKER_RE = re.compile(r"\[NOTE_ADD:\s*([^\]]+)\]")
#: 会話がメモの話題に触れていそうか（無関係な時に読み込まないためのゲート）
GATE_RE = re.compile(r"メモ|めも|note|覚え|控え|書き留め")

MAX_NOTES = 100


def available(config):
    """使える状態か。本物の連携ならAPIキーの有無をここで見る。"""
    return True


def _load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except OSError as e:
        print(f"{NAME}: メモを読めませんでした: {e}")
        return []


def _save(notes):
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(notes[-MAX_NOTES:], f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)   # 書き込み中に読まれても壊れないように


def skill_note(ctx):
    """システムプロンプトに足す「この道具の使い方」。"""
    return (
        "【共有メモ帳】メモを残してほしいと頼まれたら、返信本文の最後に改行して "
        "[NOTE_ADD: メモの本文] を付けること。頼まれていないのに付けないこと。"
    )


def context_block(ctx, gate_text):
    """回答の材料。関係なさそうなら None を返して読み込みを省く。

    別スレッドで呼ばれるので、ここにネットワークI/Oを書いてよい。
    """
    if not GATE_RE.search(gate_text or ""):
        return None
    notes = _load()
    if not notes:
        return None
    lines = "\n".join(f"- {n['text']}" for n in notes[-10:])
    return f"【共有メモ帳（直近10件）】\n{lines}"


def apply_markers(ctx, answer):
    """本文中の [NOTE_ADD: ...] を実行し、(本文, 注記) を返す。

    実際に保存できたことを -# 行で見せる。ここを省くと
    「メモしました」と言ったのに何も起きていない状態を作ってしまう。
    """
    found = MARKER_RE.findall(answer or "")
    if not found:
        return answer, []
    text = MARKER_RE.sub("", answer).strip()
    notes = _load()
    saved = []
    for raw in found:
        body = raw.strip()[:200]
        if not body:
            continue
        notes.append({"text": body, "by": ctx.agent_id})
        saved.append(body)
    if not saved:
        return text, ["-# ⚠️ メモの中身が空だったので保存していません"]
    try:
        _save(notes)
    except OSError as e:
        return text, [f"-# ⚠️ メモを保存できませんでした: {e}"]
    return text, [f"-# 📝 メモに追加: {b[:40]}" for b in saved]
