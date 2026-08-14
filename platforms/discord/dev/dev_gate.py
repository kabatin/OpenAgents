#!/usr/bin/env python3
"""開発BOT(開発BOT)の安全弁 — claude Code の PreToolUse hook（Phase 2）。

builder/gate.py（tools/配下のみ許可）を土台に、本体改修を可能にするため
書き込み許可を **scripts/ ツリー全体** へ広げる。ただし「脳と安全弁は不可侵」を
守るため、次は scripts/ 配下でも書き込みを拒否する:
  - 安全弁コード自身: dev_gate.py / deploy.py / gate.py
  - 設定・秘密: settings.json / config.json / .env / *.db
  - 常駐定義: *.plist
  - .git 直接操作

worktree隔離＋この gate＋（別途 claude --settings の Read deny）＋人間の👍最終承認、の多層で
自律改修の暴走を防ぐ。hook規約は gate.py と同じ（denyをstdoutに返し exit 0／fail-closed）。
"""

import json
import os
import re
import sys

# 書き込みを許すルート（cwd 直下のこのサブツリーのみ）
ALLOWED_SUBDIR = "scripts"
# scripts/配下でも書き込み禁止のファイル名（basename一致・大小無視）
DENY_NAMES = {"dev_gate.py", "deploy.py", "gate.py",
              "settings.json", "config.json", ".env", ".git"}
# 拡張子で拒否（常駐定義・DB実体・秘密系）
DENY_SUFFIXES = (".plist", ".db", ".db-wal", ".db-shm", ".pem", ".key")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Bashコマンド中にこれらの秘密ファイル名が現れたら拒否（catやgit経由の秘密読取事故を防ぐ）。
# Read禁止は Read ツールにしか効かないため、Bashはここで塞ぐ（多層防御）。
# "config.js" は config.json のほか `config.js*` のようなglob回避も拾う
BASH_SECRET_HINTS = ("config.js", ".env", "archive.db", "auth.json", "auth_")

# 外向き通信・持ち込み系（起票文経由のプロンプト注入による exfil / 外部コード持ち込み
# 対策）。正規の実装作業に外部通信は不要（依存が要るなら要約で申告する規約）。
# blocklistは回避可能だが、多層防御の一枚として明確な意図表明＋事故防止に効く。
# 後読みに / を含めない＝ /usr/bin/curl のようなパス付き起動も拒否する
_NET_CMD_RE = re.compile(
    r"(?<![\w.-])(curl|wget|nc|ncat|netcat|telnet|ssh|scp|sftp|rsync|ftp)"
    r"(?![\w.-])")
_PKG_INSTALL_RE = re.compile(
    r"\b(?:pip3?|npm|brew|gem|cargo)\b[^|;&]*\binstall\b")
# サブコマンド位置のみ照合（`git commit -m "fix pull request"` を誤爆しない）。
# _check_bash は入力を小文字化して照合するため IGNORECASE（-C対応）
_GIT_REMOTE_RE = re.compile(
    r"\bgit(?:\s+-c\s+\S+|\s+-\S+)*\s+(?:push|fetch|pull|clone|remote)\b",
    re.IGNORECASE)


def _target_path(tool_input):
    for key in ("file_path", "path", "notebook_path"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key]
    return None


def _check_bash(command):
    """Bashコマンドが秘密・外部通信・持ち込みに触れていないか（純粋・テスト対象）。"""
    low = (command or "").lower()
    for hint in BASH_SECRET_HINTS:
        if hint in low:
            return f"秘密ファイル({hint})に触れる可能性があるため、そのコマンドは拒否します"
    if _NET_CMD_RE.search(low):
        return ("外部ネットワークコマンド(curl/ssh等)は禁止です。"
                "外部情報が必要なら最終要約で申告してください")
    if _PKG_INSTALL_RE.search(low):
        return ("パッケージ追加(install)は禁止です。"
                "必要な依存は最終要約で申告してください")
    if _GIT_REMOTE_RE.search(low):
        return "gitのリモート操作(push/fetch/pull/clone/remote)は禁止です"
    return None


def decide(tool_name, tool_input, cwd, *, allowed_subdir=ALLOWED_SUBDIR):
    """許可(None) か 拒否理由(str) を返す（純粋関数・テスト対象）。
    - Bash: 秘密ファイル名を含むコマンドを拒否（それ以外は許可）
    - 書き込み系: cwd/scripts/ 配下のみ許可。保護名/保護拡張子は配下でも拒否
    - その他ツールは介入しない（Read禁止は claude --settings 側の責務）"""
    if tool_name == "Bash":
        return _check_bash(tool_input.get("command", ""))
    if tool_name not in WRITE_TOOLS:
        return None
    path = _target_path(tool_input)
    if not path:
        return "書き込み先が特定できないため拒否します"
    raw = path if os.path.isabs(path) else os.path.join(cwd, path)
    abspath = os.path.realpath(raw)          # symlink脱出を防ぐ
    base = os.path.basename(abspath)
    if base.lower() in {n.lower() for n in DENY_NAMES}:
        return f"{base} は保護対象（安全弁/設定/秘密）のため書き込めません"
    if base.lower().endswith(DENY_SUFFIXES):
        return f"{base} は保護対象の種別のため書き込めません"
    allowed_root = os.path.realpath(os.path.join(cwd, allowed_subdir)) + os.sep
    if not (abspath + os.sep).startswith(allowed_root):
        return (f"{allowed_subdir}/ 配下以外への書き込みは禁止です"
                f"（脳と安全弁は不可侵）: {abspath}")
    return None


def _deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    # 安全弁は fail-closed: 解釈・判定に失敗したら「拒否」に倒す
    try:
        payload = json.load(sys.stdin)
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {}) or {}
        cwd = payload.get("cwd") or os.getcwd()
        reason = decide(tool_name, tool_input, cwd)
    except Exception as e:
        _deny(f"gate内部エラーのため安全側で拒否: {e}")
        return
    if reason:
        _deny(reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
