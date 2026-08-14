#!/usr/bin/env python3
"""
builder の安全弁（エージェントv2 Phase 3）。設計: docs/agents-v2-design.md §6.1, §8

claude Code の PreToolUse hook として使う。builder（Bash/Edit/Write許可の
claude -p）が書き込んでよいのは worktree 内の tools/ 配下だけ。runner・bot.py・
gate.py 自身・settings.json・personas 等（＝脳と安全弁）への書き込みは拒否する。
「手足は生やさせ、脳と安全弁は触らせない」を物理的に強制する。

hook規約: stdin から {tool_name, tool_input:{...}} のJSONを受け取り、
拒否する場合は permissionDecision=deny を JSON で stdout に返して exit 0。
許可は空出力（フックは介入しない）で exit 0。

設定は builder が使う settings.json の hooks.PreToolUse から:
    { "type": "command", "command": "python3 <このファイル>" }
"""

import json
import os
import sys

# 書き込みを許すのは cwd 直下の tools/ のみ（それ以外は全て拒否）
ALLOWED_SUBDIR = "tools"
# 明示的に触らせないファイル/ディレクトリ（多層防御。tools/配下でも下記名は拒否）
DENY_NAMES = {"gate.py", "settings.json", ".git"}
# 書き込み系ツール
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _target_path(tool_input):
    """tool_input から書き込み先パスを取り出す（ツールごとにキーが違う）。"""
    for key in ("file_path", "path", "notebook_path"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key]
    return None


def decide(tool_name, tool_input, cwd):
    """許可(None) か 拒否理由(str) を返す（純粋関数・テスト対象）。
    - 書き込み系ツール以外は介入しない（Bash等はサンドボックス側の責務）
    - 書き込み先が cwd/tools/ 配下でなければ拒否
    - tools/配下でも DENY_NAMES に該当すれば拒否"""
    if tool_name not in WRITE_TOOLS:
        return None
    path = _target_path(tool_input)
    if not path:
        return "書き込み先が特定できないため拒否します"
    # realpath で symlink を解決してから判定（symlink脱出を防ぐ）
    raw = path if os.path.isabs(path) else os.path.join(cwd, path)
    abspath = os.path.realpath(raw)
    if os.path.basename(abspath).lower() in {n.lower() for n in DENY_NAMES}:
        return f"{os.path.basename(abspath)} は保護対象のため書き込めません"
    allowed_root = os.path.realpath(os.path.join(cwd, ALLOWED_SUBDIR)) + os.sep
    if not (abspath + os.sep).startswith(allowed_root):
        return (f"tools/ 配下以外への書き込みは禁止です（脳と安全弁は不可侵）: "
                f"{abspath}")
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
