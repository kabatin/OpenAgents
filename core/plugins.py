#!/usr/bin/env python3
"""
プラグインスキル機構（エージェントv2 Phase 3）。設計: docs/agents-v2-design.md §6

builder が生成する「新しい能力（ツール）」の置き場と読み込み。
tools/*.py を動的に読み込み、既存のマーカー方式（[RULE:]/[HIRE:] 等）と同じ
イディオムで、bot.py を編集せずに能力を追加できるようにする。

プラグインの規約（tools/<name>.py が定義する）:
    NAME       = "weather"                 # 一意な識別子（ファイル名と一致推奨）
    MARKER     = "WEATHER"                 # 回答に埋める [WEATHER: 引数] のキー
    SKILL_NOTE = "…（LLMへの使い方説明）"
    def handle(arg: str) -> str: ...       # マーカーの中身を受け、投稿する文字列を返す

安全性（多層防御）:
- 生成物は static_check（AST）で危険なimport/呼び出し（os/subprocess/socket/eval等）を
  排除してからでないと実行しない。ただし完全なサンドボックスではない。
- builderはworktree隔離で生成し、テスト＋人間承認を経たものだけが live tools/ に入る。
- 予約マーカー（組み込みの [RULE:]/[HIRE:] 等）は使えない。
- handle は botプロセス内で実行されるため、apply_markers は別スレッド＋handle毎に
  タイムアウトを掛け、暴走・長考が本処理（イベントループ）を巻き込まないようにする。

単体テスト: ./venv/bin/python -m unittest discover -v
"""

import ast
import concurrent.futures
import glob
import importlib.util
import os
import re
from core import paths

TOOLS_DIR = paths.TOOLS_DIR

HANDLE_TIMEOUT_SEC = 8
HANDLE_TIMEOUT_NOTE = "（ツールの実行がタイムアウト/失敗しました）"
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)
MAX_MARKER_RUNS = 6      # 1回答あたりのツール実行数上限（DoS抑止）
MAX_MARKER_ARG = 2000    # マーカー引数の最大長
MAX_MARKER_OUT = 1500    # ツール1件の出力最大長

# 組み込みマーカーと衝突させない（権限の取り違え防止）。builderツールはこれ以外を使う
RESERVED_MARKERS = {"IMAGE", "CAPTION", "RULE", "RULE_CANCEL", "REMIND",
                    "REMIND_CANCEL", "CAPABILITY", "HIRE", "FIRE"}

# プラグインは「純粋ロジック限定」。ネットワーク(urllib/socket)・ファイル(open)・
# os/subprocess は一切禁止（承認済みでもbotプロセス内で実行されるため、secret
# 読取・外部送信・安全弁上書きを構造的に不可能にする）。AST静的検査は原理的に
# 完璧なサンドボックスではないので、import allowlist＋dunder禁止＋モジュール直下
# の副作用（Call）禁止で「実行可能な範囲」を計算に限定する多層防御とする。
SAFE_MODULES = {
    "re", "math", "json", "datetime", "zoneinfo", "unicodedata", "string",
    "textwrap", "html", "decimal", "fractions", "statistics", "collections",
    "itertools", "functools", "typing", "dataclasses", "enum", "calendar",
    "random", "difflib", "bisect", "heapq", "operator", "numbers",
}
# 呼び出し名で禁止する組み込み（サンドボックス脱出・任意コード実行の起点）
_DANGER_CALLS = {
    "eval", "exec", "compile", "__import__", "breakpoint", "open",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "input", "memoryview", "help",
}
# テストが追加で使ってよいモジュール（ツール本体には許可しない）
TEST_EXTRA_MODULES = frozenset({"unittest", "importlib"})


def static_check(source, *, extra_modules=frozenset(), allow_toplevel_call=False):
    """AST でツールの安全性を静的検査する（純粋関数・テスト対象）。
    問題があれば理由文字列、無ければ None。
    - import は allowlist のみ（ネットワーク/os/subprocess/ファイルを排除）
    - dunder属性アクセス（__class__/__subclasses__ 等のサンドボックス脱出）を一律禁止
    - open/getattr/eval 等の危険な組み込み呼び出しを禁止
    - モジュール直下（import時に走る）の関数呼び出しを禁止し副作用を封じる
      （テスト側は allow_toplevel_call=True で緩める）"""
    try:
        tree = ast.parse(source or "")
    except SyntaxError as e:
        return f"構文エラー: {e}"
    allowed = SAFE_MODULES | set(extra_modules)
    if not allow_toplevel_call:
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                return "モジュール直下での関数呼び出しは禁止（import時副作用の防止）"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in allowed:
                    return f"許可されていないimport: {a.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in allowed:
                return f"許可されていないimport: {node.module}"
        elif isinstance(node, ast.Attribute):
            # dunderトラバーサル封殺（().__class__.__subclasses__() 等）
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return f"dunder属性アクセスは禁止: .{node.attr}"
        elif isinstance(node, ast.Call):
            nm = getattr(node.func, "id", None)
            if nm in _DANGER_CALLS:
                return f"危険な呼び出し: {nm}"
    return None


def _valid_marker(m):
    return (isinstance(m, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", m or "")
            and m not in RESERVED_MARKERS)


def _load_one(path, plugins):
    """1ファイルを読み込んでpluginsに登録（規約外/壊れ/静的検査NGは無視）。
    ロード＝module-levelを実行するため、先に static_check で危険コードを排除する。"""
    base = os.path.basename(path)
    name = base[:-3]
    try:
        with open(path, encoding="utf-8") as f:
            err = static_check(f.read())
        if err:
            print(f"skip unsafe plugin {base}: {err}")
            return
        spec = importlib.util.spec_from_file_location(f"tools.{name}", path)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        marker = getattr(mod, "MARKER", None)
        if not _valid_marker(marker) or not callable(
                getattr(mod, "handle", None)):
            print(f"skip invalid plugin: {base}")
            return
        if marker in plugins:
            print(f"duplicate plugin marker {marker} in {base}, skip")
            return
        plugins[marker] = mod
    except Exception as e:
        print(f"plugin load failed ({base}): {e}")


def load_plugins(tools_dir=TOOLS_DIR):
    """tools/*.py を読み込み {MARKER: module} を返す。壊れた/規約外/危険は無視。
    先頭 _ のファイルとテストは読み込まない。"""
    plugins = {}
    if not os.path.isdir(tools_dir):
        return plugins
    for path in sorted(glob.glob(os.path.join(tools_dir, "*.py"))):
        base = os.path.basename(path)
        if base.startswith("_") or base.startswith("test_"):
            continue
        _load_one(path, plugins)
    if plugins:
        print(f"plugins loaded: {sorted(plugins)}")
    return plugins


def load_one_plugin(path):
    """単一ファイルだけをプラグインとして読む（builder/activateの検証用。
    他ファイルを exec しない＝『昇格する1枚だけ実行』を保証）。"""
    plugins = {}
    _load_one(path, plugins)
    return plugins


def build_skill_notes(plugins):
    """読み込み済みプラグインの SKILL_NOTE を連結（無ければ空文字）。"""
    notes = []
    for marker, mod in sorted(plugins.items()):
        note = getattr(mod, "SKILL_NOTE", "").strip()
        if note:
            notes.append(note)
    return "\n".join(notes)


def _marker_re(marker):
    return re.compile(r"\[" + re.escape(marker) + r":\s*([^\]]+)\]")


def apply_markers(plugins, answer):
    """回答から各プラグインの [MARKER: 引数] を除去し handle() を実行する。
    返り値 (本文, 実行結果テキスト[])。handle の例外は握って結果に残す。
    副作用（投稿等）は handle 内では行わず、返り値テキストを呼び出し側が扱う。"""
    text = answer or ""
    results = []
    runs = 0
    for marker, mod in plugins.items():
        rx = _marker_re(marker)
        for m in rx.finditer(text):
            if runs >= MAX_MARKER_RUNS:      # 1回答あたりの実行数上限（DoS抑止）
                break
            runs += 1
            arg = m.group(1).strip()[:MAX_MARKER_ARG]
            fut = None
            try:
                # handle毎にタイムアウト（暴走・長考が本処理を巻き込まないよう
                # 別スレッド実行）。タイムアウト時は未開始タスクをcancelして
                # プール枯渇を抑える（実行中スレッドはプリエンプト不能＝残留し得る）
                fut = _EXECUTOR.submit(mod.handle, arg)
                out = fut.result(timeout=HANDLE_TIMEOUT_SEC)
                if out:
                    results.append(str(out)[:MAX_MARKER_OUT])
            except concurrent.futures.TimeoutError:
                if fut is not None:
                    fut.cancel()
                print(f"plugin {marker} handle timed out")
                results.append(HANDLE_TIMEOUT_NOTE)
            except Exception as e:
                print(f"plugin {marker} handle failed: {e}")
                results.append(HANDLE_TIMEOUT_NOTE)
        text = rx.sub("", text)
    return text.strip(), results
