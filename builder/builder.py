#!/usr/bin/env python3
"""
builder — 自己進化の実行体（エージェントv2 Phase 3）。設計: docs/agents-v2-design.md §6

能力起票(capability_request)を1件受け、git worktreeで隔離した claude -p
（Bash/Edit/Write許可＋gate.pyフック）に、tools/ 配下のプラグインツール＋テストを
生成させ、pytestで検証する。**live には一切触れない**（隔離＋人間承認が安全弁）。

人間トリガー: ./venv/bin/python builder.py <capability_request_id>
出力(JSON): worktree・生成ファイル・marker・テスト結果・diff。
これを人間が確認し、OKなら activate.py で live tools/ に反映する（Phase 3-D）。

安全モデル:
- worktree隔離: 生成物は本番と別ディレクトリ。liveのbot.py等には波及しない
- gate.py(PreToolUse hook): tools/配下以外へのWrite/Editを拒否
- activate は tools/<name>.py 1枚だけを live へ運ぶ（他は運ばない）
- 検証(pytest)＋人間の差分確認を通過したものだけ採用
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = subprocess.run(
    ["git", "-C", HERE, "rev-parse", "--show-toplevel"],
    capture_output=True, text=True).stdout.strip()
# chatbot の repo 相対パス（worktree内でも同じ）
DB_PATH = os.path.join(REPO_ROOT, "state", "archive.db")
GATE_PY = os.path.join(HERE, "gate.py")            # liveのgate（worktree外）を使う
# テスト実行はliveのvenv（worktreeにはvenvが無い＝gitignore）を使う
# venv の場所は OS で違うので core.paths に判定を集約している
VENV_PY = None   # 実行時に _venv_python() で解決する
# コード生成モデル。生成物が本番に入るため、最も慎重なOpusを使う
BUILDER_MODEL = "claude-opus-4-8"
BUILD_TIMEOUT_SEC = 600


def _venv_python():
    sys.path.insert(0, REPO_ROOT)
    from core import paths
    return paths.venv_python()


def _claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _load_request(req_id):
    sys.path.insert(0, REPO_ROOT)
    from core import db
    with db.connect(DB_PATH) as conn:
        return db.get_capability_request(conn, req_id)


def build_prompt(req):
    """builderへの指示（純粋関数・テスト対象）。プラグイン規約を厳守させる。"""
    return f"""あなたはツール開発者です。次の能力起票を満たす「プラグインツール」を
1つだけ実装してください。

【能力起票 #{req['id']}】
{req['description']}
（文脈: {req.get('context') or 'なし'}）

【厳守する規約】
- 実装先は tools/ ディレクトリのみ。他のファイル（bot.py, runner/, gate.py,
  settings.json, personas/ 等）には絶対に書き込まない（拒否される）。
- tools/<name>.py を新規作成し、次の規約に沿うこと:
    NAME = "<name>"                     # ファイル名と同じ小文字英数字
    MARKER = "<MARKER>"                 # 大文字英数字。既存と被らない
    SKILL_NOTE = "…"                    # LLMが「いつ [MARKER: 引数] を出すか」の説明
    def handle(arg: str) -> str: ...    # マーカー引数を受け、投稿する文字列を返す
  handle は副作用（外部投稿・ファイル書込・ネットワーク）を一切行わず、
  引数の文字列から純粋計算で結果の文字列を返すだけにする。
  使ってよいのは純粋な標準ライブラリのみ（re/math/json/datetime/collections/
  itertools/random/statistics 等）。os・subprocess・socket・urllib・open は
  使用禁止（静的検査で拒否される）。例外は握って分かりやすいメッセージ文字列を返す。
- 同じディレクトリに tools/test_<name>.py を作り、handle と純粋ロジックを
  unittest で検証する（ネットワークに依存しない範囲で）。テストは
  `import importlib.util` で tools/<name>.py を読み込む形にする
  （相対importに依存しない）。
- テストの実行はビルド係が行うので、あなたはファイルを2枚書けばよい。

作業ディレクトリは discord-archive です。tools/ 配下だけを touch してください
（他のファイルへの書き込みは拒否されます）。"""


def _run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


def build(req_id):
    req = _load_request(req_id)
    if req is None:
        return {"ok": False, "error": f"capability_request #{req_id} が無い"}

    branch = f"cap/{req_id}"
    wt = os.path.join(os.path.dirname(REPO_ROOT),
                      f"{os.path.basename(REPO_ROOT)}-wt-cap-{req_id}")
    # 既存worktreeは掃除してからやり直す
    _run(["git", "-C", REPO_ROOT, "worktree", "remove", "--force", wt])
    _run(["git", "-C", REPO_ROOT, "branch", "-D", branch])
    add = _run(["git", "-C", REPO_ROOT, "worktree", "add", "-b", branch, wt])
    if add.returncode != 0:
        return {"ok": False, "error": f"worktree作成失敗: {add.stderr[:300]}"}

    archive_dir = os.path.join(wt, "core")
    tools_before = set(os.listdir(os.path.join(archive_dir, "tools")))

    # gate.py を PreToolUse フックに仕込む＋secret類のReadを禁止（多層防御）
    settings = json.dumps({
        "permissions": {"deny": [
            "Read(~/**)", "Read(**/config.json)", "Read(**/.env)",
            "Read(**/*.db)", "Read(**/auth*.json)"]},
        "hooks": {"PreToolUse": [
            {"matcher": "Write|Edit|MultiEdit|NotebookEdit",
             "hooks": [{"type": "command",
                        "command": f"python3 {GATE_PY}"}]}]}})
    # Bashは渡さない（テストはbuilder側で実行）。gate.pyがtools/外Writeを拒否。
    argv = [_claude_bin(), "-p", "--model", BUILDER_MODEL,
            "--output-format", "stream-json", "--verbose",
            "--tools", "Read,Write,Edit,Grep,Glob",
            "--permission-mode", "acceptEdits",
            "--settings", settings]
    try:
        proc = subprocess.run(argv, cwd=archive_dir, input=build_prompt(req),
                              capture_output=True, text=True,
                              timeout=BUILD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "builderがタイムアウト", "worktree": wt}

    # 生成された tools/*.py を特定（本体・テスト）
    tools_after = set(os.listdir(os.path.join(archive_dir, "tools")))
    added = tools_after - tools_before
    new_files = sorted(f for f in added
                       if f.endswith(".py") and not f.startswith("test_"))
    diff = _run(["git", "-C", wt, "diff", "--stat"]).stdout

    if not new_files:
        return {"ok": False, "error": "tools/ に新規ツールが作られなかった",
                "worktree": wt, "diff": diff,
                "claude_tail": (proc.stdout or "")[-500:]}
    # 安全弁: 新規ツール本体は「ちょうど1枚」に限定（複数はレビューと昇格がズレる）
    if len(new_files) != 1:
        return {"ok": False, "error": f"新規ツールが複数作られました: {new_files}",
                "worktree": wt, "diff": diff}

    tool_file = new_files[0]
    name = tool_file[:-3]
    tools_path = os.path.join(archive_dir, "tools")
    with open(os.path.join(tools_path, tool_file), encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'MARKER\s*=\s*[\'"]([A-Z][A-Z0-9_]{1,31})[\'"]', src)
    marker = m.group(1) if m else None

    # 実行前の静的検査（AST）: 危険なimport/dunder/呼び出しがあれば実行せず中止。
    # 本体・テストの両方を検査してから初めてテストを走らせる（生成物を無検査で
    # exec しないための CRITICAL 対策）。テストは unittest/importlib のみ追加許可
    # （os は許可しない＝テスト経由のRCEを断つ）。テスト側はメソッド内Callが要る
    # ため allow_toplevel_call=True。
    live_archive = os.path.join(REPO_ROOT, "core")
    sys.path.insert(0, live_archive)
    import plugins as _pl
    tool_err = _pl.static_check(src)
    test_path = os.path.join(tools_path, f"test_{name}.py")
    test_src = open(test_path).read() if os.path.exists(test_path) else ""
    test_err = _pl.static_check(
        test_src, extra_modules=_pl.TEST_EXTRA_MODULES,
        allow_toplevel_call=True)
    if tool_err or test_err:
        return {"ok": False, "worktree": wt, "tool_file": f"tools/{tool_file}",
                "error": f"静的検査で拒否: 本体={tool_err} テスト={test_err}"}

    # 1) 新規ツールのテスト（builderが書いたもの）
    test = _run([_venv_python(), "-m", "unittest", f"tools.test_{name}"],
                cwd=archive_dir, timeout=300)

    # 2) プラグイン健全性: 昇格する1枚だけを読み（他ファイルはexecしない）、
    #    markerがliveと衝突しないか検証する
    wt_tool = os.path.join(archive_dir, "tools", tool_file)
    check_code = (
        "import sys;"
        f"sys.path.insert(0, {live_archive!r});"
        "import plugins;"
        f"p = plugins.load_one_plugin({wt_tool!r});"
        "live = plugins.load_plugins();"
        f"m = {marker!r};"
        "assert m in p, 'markerが読み込めない（規約違反/静的検査NGの可能性）';"
        "assert m not in live, 'markerが既存ツールと衝突';"
        "print('plugin-ok')")
    check = _run([_venv_python(), "-c", check_code], cwd=live_archive, timeout=60)
    plugin_ok = check.returncode == 0 and "plugin-ok" in check.stdout

    return {
        "ok": test.returncode == 0 and plugin_ok and marker is not None,
        "request": req["description"],
        "worktree": wt,
        "tool_file": f"tools/{tool_file}",
        "name": name,
        "marker": marker,
        "test_passed": test.returncode == 0,
        "plugin_ok": plugin_ok,
        "test_output": (test.stderr or test.stdout)[-600:],
        "plugin_check": (check.stderr or check.stdout)[-300:],
        "diff": _run(["git", "-C", wt, "diff", "--stat"]).stdout,
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: builder.py <capability_request_id>")
    result = build(int(sys.argv[1]))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
