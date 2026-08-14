#!/usr/bin/env python3
"""
activate — builderが作ったツールを承認して live に反映（エージェントv2 Phase 3）。

builder.py の出力（worktree）を人間が確認し、OKならこのスクリプトで有効化する。
**live へ運ぶのは tools/<name>.py 1枚だけ**（worktreeの他の変更は一切運ばない）。
これが安全弁: builderがworktree内で何をしても、liveに届くのはツール本体のみ。

  ./venv/bin/python activate.py <capability_request_id>

処理: worktreeの tools/<name>.py を live tools/ にコピー → tool_registry登録 →
capability_request を deployed に → worktree掃除。
反映後、bot は次のイベントで load_plugins() すれば再起動なしで使える
（launchctl kickstart でも可）。

却下する場合: ./venv/bin/python activate.py <id> --reject （worktree掃除のみ）
"""

import argparse
import datetime
import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = subprocess.run(
    ["git", "-C", HERE, "rev-parse", "--show-toplevel"],
    capture_output=True, text=True).stdout.strip()
# 生成物（プラグイン）の置き場は core/tools。worktree 内でも同じ相対位置
TOOLS_REL = os.path.join("core", "tools")
LIVE_TOOLS = os.path.join(REPO_ROOT, TOOLS_REL)
DB_PATH = os.path.join(REPO_ROOT, "state", "archive.db")

sys.path.insert(0, REPO_ROOT)
from core import db          # noqa: E402
from core import plugins     # noqa: E402


def _worktree(req_id):
    return os.path.join(os.path.dirname(REPO_ROOT),
                        f"{os.path.basename(REPO_ROOT)}-wt-cap-{req_id}")


def _cleanup(wt, branch):
    subprocess.run(["git", "-C", REPO_ROOT, "worktree", "remove",
                    "--force", wt], capture_output=True)
    subprocess.run(["git", "-C", REPO_ROOT, "branch", "-D", branch],
                   capture_output=True)


def activate(req_id):
    wt = _worktree(req_id)
    branch = f"cap/{req_id}"
    wt_tools = os.path.join(wt, TOOLS_REL)
    if not os.path.isdir(wt_tools):
        return f"worktreeが見つかりません（先に builder.py {req_id} を実行）: {wt}"

    # worktreeに追加された tools/<name>.py（テスト除く）を特定
    live_names = {os.path.basename(p) for p in
                  glob.glob(os.path.join(LIVE_TOOLS, "*.py"))}
    candidates = [f for f in os.listdir(wt_tools)
                  if f.endswith(".py") and not f.startswith(("test_", "_"))
                  and f not in live_names]
    if not candidates:
        return "worktreeに新規ツールが見つかりません"
    if len(candidates) != 1:      # 安全弁: レビューと昇格のズレを防ぐ
        return f"新規ツールが1枚に絞れません（{candidates}）。手動確認してください"
    tool_file = candidates[0]
    name = tool_file[:-3]
    src = os.path.join(wt_tools, tool_file)

    # 有効化前の最終検証: 静的検査→単一プラグイン読込（他ファイルはexecしない）
    # →marker衝突（予約・live）
    with open(src, encoding="utf-8") as f:
        source = f.read()
    err = plugins.static_check(source)
    if err:
        return f"静的検査で拒否（有効化中止）: {err}"
    p = plugins.load_one_plugin(src)
    m = re.search(r'MARKER\s*=\s*[\'"]([A-Z][A-Z0-9_]{1,31})[\'"]', source)
    marker = m.group(1) if m else None
    if marker is None or marker not in p:
        return "ツールがプラグイン規約を満たしません（有効化中止）"
    if marker in plugins.load_plugins():   # live に同markerが既にある
        return f"marker {marker} は既存ツールと衝突します（有効化中止）"

    # 台帳登録を先に確定 → その後 live tools/ に「本体1枚だけ」を原子的に配置
    # （順序を逆にすると、コピー後のDB失敗で「未登録なのに稼働」不整合が残る。
    #  copyは一時ファイル→os.replaceで原子的に）
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with db.connect(DB_PATH) as conn:
        db.register_tool(conn, name=name, marker=marker, source_req=req_id,
                         created_at=now)
        db.set_capability_status(conn, req_id, "deployed")
    dest = os.path.join(LIVE_TOOLS, tool_file)
    tmp = dest + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
    _cleanup(wt, branch)
    return (f"✅ 有効化: {tool_file}（marker={marker}）を live に反映し登録しました。\n"
            f"   bot に反映: launchctl kickstart -k gui/$(id -u)/com.discord.archivebot")


def reject(req_id):
    _cleanup(_worktree(req_id), f"cap/{req_id}")
    with db.connect(DB_PATH) as conn:
        db.set_capability_status(conn, req_id, "rejected")
    return "却下: worktreeを掃除し、起票を rejected にしました。"


def main():
    ap = argparse.ArgumentParser(description="builderツールの承認/有効化")
    ap.add_argument("req_id", type=int)
    ap.add_argument("--reject", action="store_true")
    args = ap.parse_args()
    print(reject(args.req_id) if args.reject else activate(args.req_id))


if __name__ == "__main__":
    main()
