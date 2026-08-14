#!/usr/bin/env python3
"""開発BOT(開発BOT)の改修パイプライン（Phase 2）。

起票(capability_request)を1件受け、git worktree隔離で claude -p(Opus,
Bash/Edit/Write許可＋dev_gateフック) に本体改修＋テストを実装させ、
stream-jsonを行読みして進捗を出し、テスト＋pyflakesで検証、diff/サマリーを作る。
**live には触れない**（worktree隔離＋人間の👍最終承認が安全弁。deployはPhase 3）。

純粋関数（parse_dev_command / build_prompt / classify_event / summarize /
ProgressBuffer）とIO（worktree・stream_claude・tests・pyflakes・diff）を分離する。
"""

import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time

from core import paths

MODEL = "claude-opus-4-8"          # 本体改修は最も慎重なOpus
# 30分。Opusの実装は12分超が普通にある（起票#7は900秒の壁時計をほぼ使い切った）
BUILD_TIMEOUT_SEC = 1800
# ハング保険。--include-partial-messages で長考中もstreamは流れるが、長いツール実行
# （テスト一式など）中は無音になる。180秒では書き終わり間際の無音で誤発動した（起票#7）。
IDLE_TIMEOUT_SEC = 600

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = subprocess.run(
    ["git", "-C", HERE, "rev-parse", "--show-toplevel"],
    capture_output=True, text=True).stdout.strip()
REPO_REL = "."          # claude の cwd（各BOTのフォルダが直下にある）
ARCHIVE_REL = "core"
MEETING_REL = "platforms/discord/meeting"
# venv の場所は OS で違う（Windows は Scripts\python.exe）。core.paths が吸収する
LIVE_VENV_PY = paths.venv_python()
MEETING_VENV_PY = LIVE_VENV_PY
DEV_GATE_PY = os.path.join(HERE, "dev_gate.py")        # liveの安全弁（worktree外）を使う


# ----------------------------------------------------------------- 純粋関数
def parse_dev_command(content):
    """AI開発室の発言から改修対象の起票idを取り出す（純粋関数）。
    受理: `[DEV: 4]` / `起票 #4 やって` / `起票4 実装`。該当なしは None。"""
    text = content or ""
    m = re.search(r"\[DEV:\s*(\d+)\]", text)
    if m:
        return int(m.group(1))
    if "起票" in text:
        m = re.search(r"起票\s*#?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    return None


# 破棄は不可逆なので、明確な意図表明の語だけに絞る（「最初からテストを書いて」
# 「設計をやり直した方が…」のような自然文での誤爆でworktreeを消さない）
_FRESH_RE = re.compile(r"作り直し|作りなおし|ゼロから")


def wants_fresh_start(text):
    """「作り直し」系の指示か（純粋関数）。Trueなら前回worktreeを破棄してゼロから。
    指定が無ければ、failed/interruptedの残骸があるときは続きから再開する。"""
    return bool(_FRESH_RE.search(text or ""))


# 実装規約は「データとしてのルール」＝ dev-guidelines.md に置く。開発BOT自身が
# 起票→👍承認でこのファイルを改善できる（コード変更・再起動が不要になる）。
GUIDELINES_PATH = os.path.join(HERE, "dev-guidelines.md")
# ファイル欠損時も改修が止まらないための最小規約（フォールバック）
DEFAULT_GUIDELINES = """\
- まず既存の関連実装を Grep/Glob/Bash で探して読み、既存モジュールへ統合する。
  孤立した新規ファイルを作るのは、本当にそれが正しい設計の時だけにする。
- 既存のコード規約・命名に合わせ、差分は最小限に。純粋関数とIOを分離する。
- 秘密ファイル（config.json / .env / *.db / auth*）は読まない・出力しない。
  外部ネットワークアクセスと pip install は禁止（必要な依存は要約で申告）。
- 振る舞いを変えたら対応するテストを追加/更新して緑を確認する。
- 変更してよいのは scripts/ 配下のみ（dev_gate.py / deploy.py / gate.py /
  settings.json / config.json / .env / *.plist / *.db への書き込みは拒否される）。
- 最後に「変更したファイル」と「何をどう変えたか」を3〜6行で要約する。"""


def load_guidelines(path=GUIDELINES_PATH):
    """実装規約をデータファイルから読む（IO）。欠損・空なら内蔵の最小規約。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        return text or DEFAULT_GUIDELINES
    except OSError:
        return DEFAULT_GUIDELINES


_LESSON_LABEL = {"failed": "失敗", "rejected": "却下", "note": "メモ"}


def format_lessons(lessons):
    """教訓リスト [{kind,text}] をプロンプト注入用ブロックへ（純粋関数）。
    空・中身なしなら空文字（プロンプトに余計なセクションを作らない）。"""
    lines = [f"- [{_LESSON_LABEL.get(l.get('kind'), l.get('kind'))}] "
             f"{l['text'][:200]}"
             for l in (lessons or []) if (l.get("text") or "").strip()]
    if not lines:
        return ""
    return "【過去の教訓（同じ失敗を繰り返さない）】\n" + "\n".join(lines)


def build_prompt(cap_req, guidelines=None, lessons=None, resume=False):
    """claude への改修指示（純粋関数・テスト対象）。規約本文は guidelines
    （load_guidelines が読んだデータ）、lessons は過去の教訓 [{kind,text}]。
    resume=True は中断ジョブの再開（worktreeの途中経過から続ける）。"""
    rules = (guidelines or DEFAULT_GUIDELINES).strip()
    lessons_block = format_lessons(lessons)
    if lessons_block:
        lessons_block = f"\n{lessons_block}\n"
    resume_block = ""
    if resume:
        resume_block = """
【前回からの続き】
前回の実装が途中で中断され、worktreeに途中までの変更が残っている。
まず `git status` と `git diff HEAD` で現状を把握し、ゼロから書き直さず
続きから完成させること（正しく書けている部分はそのまま活かす）。
"""
    return f"""あなたはこのリポジトリの開発者です。次の能力起票を満たすよう実装してください。

【能力起票 #{cap_req['id']}】
{cap_req['description']}
（文脈: {cap_req.get('context') or 'なし'}）

【実装規約（must）】
{rules}
{lessons_block}{resume_block}
【テスト実行】
- unittest は `{LIVE_VENV_PY} -m unittest ...` で回せる。

作業ディレクトリはリポジトリのルートです。BOTのフォルダ配下だけを触ってください。"""


def classify_event(ev):
    """stream-json イベント1件を短い進捗ラベルへ（純粋関数）。出さないものは None。"""
    if not isinstance(ev, dict) or ev.get("type") != "assistant":
        return None
    for block in (ev.get("message") or {}).get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        inp = block.get("input") or {}
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            f = inp.get("file_path") or inp.get("path") or "?"
            return f"✏️ 編集 {os.path.basename(f)}"
        if name == "Bash":
            cmd = (inp.get("command") or "").strip().splitlines()[0][:50]
            return f"🔧 実行 {cmd}"
        if name in ("Read", "Grep", "Glob", "TodoWrite"):
            return None                       # 読み取り/内部管理はノイズなので出さない
        return f"🛠 {name}"
    return None


def summarize(cap_req, *, test_ok, test_tail, flakes_ok, flakes_tail,
              diff_stat, final_text, error=None, salvaged=False,
              warnings=None):
    """👍承認待ちに出す最終サマリー（純粋関数）。
    salvaged=True は「中断エラーはあったが差分あり＋検証緑」の回収ケース＝
    失敗として捨てず承認待ちに乗せる（起票#7で完成品をfailed扱いした反省）。
    warnings は risk_warnings() の注意書き（承認者に必ず見せる）。"""
    if error and not salvaged:
        head = f"❌ 起票#{cap_req['id']} の実装に失敗しちゃいました…ごめんなさい🙇"
        body = [f"理由: {error}"]
        if diff_stat.strip():
            body.append(f"（途中まで書いた差分です）\n```\n{diff_stat.strip()[:800]}\n```")
        body.append(f"（もう一回『起票 #{cap_req['id']} やって』で**続きから**再挑戦できます）")
        return head + "\n" + "\n".join(body)
    test_mark = "🟢" if test_ok else "🔴"
    flakes_mark = "🟢" if flakes_ok else "🔴"
    lines = [
        f"🧵 **起票#{cap_req['id']} 実装できました！（承認待ち）**",
        f"> {cap_req['description'][:120]}",
        "",
    ]
    if error and salvaged:
        lines += [
            f"⚠️ 実装は途中で中断しました（{error}）。でも差分あり＋検証緑だったので"
            "承認候補として出します。中身は特に注意して見てほしいです🙏",
            "",
        ]
    if warnings:
        lines += list(warnings) + [""]
    lines += [
        f"{test_mark} テスト {'緑' if test_ok else '赤（要確認）'}／"
        f"{flakes_mark} pyflakes {'clean' if flakes_ok else 'warning'}",
        "",
        "**変更ファイル**",
        "```",
        (diff_stat.strip() or "（差分なし）")[:1000],
        "```",
    ]
    if final_text:
        lines += ["**実装者の要約**", final_text[:800]]
    if not test_ok and test_tail:
        lines += ["<テスト末尾>", "```", test_tail[-600:], "```"]
    if not flakes_ok and flakes_tail:
        lines += ["<pyflakes>", "```", flakes_tail[-400:], "```"]
    lines += ["", "先輩、👍でlive反映（対象BOT再起動込み）、👎で破棄です〜"]
    return "\n".join(lines)


class ProgressBuffer:
    """claude実行スレッドが書き、Discord側が読む進捗の共有状態（スレッド安全）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._phase = "準備中"
        self._ops = 0
        self._last = ""

    def set_phase(self, phase):
        with self._lock:
            self._phase = phase

    def add_op(self, label):
        with self._lock:
            self._ops += 1
            self._last = label

    def snapshot(self):
        with self._lock:
            return self._phase, self._ops, self._last

    def render(self):
        phase, ops, last = self.snapshot()
        tail = f"／直近: {last}" if last else ""
        return f"🛠 {phase}（{ops}操作{tail}）"


# ----------------------------------------------------------------- IO
def _claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


def worktree_paths(req_id):
    branch = f"cap-dev/{req_id}"
    wt = os.path.join(os.path.dirname(REPO_ROOT),
                      f"{os.path.basename(REPO_ROOT)}-wt-dev-{req_id}")
    cwd = os.path.join(wt, REPO_REL)
    archive_dir = os.path.join(wt, ARCHIVE_REL)
    return branch, wt, cwd, archive_dir


def create_worktree(req_id):
    """既存を掃除してから worktree を作る。(branch, wt, cwd, archive_dir) を返す。"""
    branch, wt, cwd, archive_dir = worktree_paths(req_id)
    _run(["git", "-C", REPO_ROOT, "worktree", "remove", "--force", wt])
    _run(["git", "-C", REPO_ROOT, "branch", "-D", branch])
    add = _run(["git", "-C", REPO_ROOT, "worktree", "add", "-b", branch, wt])
    if add.returncode != 0:
        raise RuntimeError(f"worktree作成失敗: {add.stderr[:300]}")
    return branch, wt, cwd, archive_dir


def remove_worktree(wt, branch):
    _run(["git", "-C", REPO_ROOT, "worktree", "remove", "--force", wt])
    _run(["git", "-C", REPO_ROOT, "branch", "-D", branch])


def worktree_usable(wt):
    """既存worktreeが「続きから」に使える状態か（IO）。
    ディレクトリがあり、gitとして生きていれば再開できる。"""
    if not os.path.isdir(wt):
        return False
    r = _run(["git", "-C", wt, "rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0 and r.stdout.strip() == "true"


def dev_settings():
    """claude --settings 用。Bashを事前承認（acceptEditsはBashを自動承認しないため）、
    secret類のReadを禁止、dev_gateをWrite/Edit/BashのPreToolUseに仕込む（多層防御）。"""
    return json.dumps({
        "permissions": {
            "allow": ["Bash"],
            "deny": [
                "Read(~/**)", "Read(**/config.json)", "Read(**/.env)",
                "Read(**/*.db)", "Read(**/auth*.json)"]},
        "hooks": {"PreToolUse": [
            {"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
             "hooks": [{"type": "command",
                        "command": f"python3 {DEV_GATE_PY}"}]}]}})


def claude_argv(bin_path, model=MODEL):
    """claude -p の引数列（純粋関数・テスト対象）。
    - Bashは dev_settings の allow で事前承認する（acceptEditsだけではBashが自動承認
      されないため）。探索(grep)・テスト実行に使えるようにして統合品質を上げる。
    - --include-partial-messages で長考中もstreamを流し、idle検知（無音=ハング扱い）の
      誤発動を防ぐ。"""
    return [bin_path, "-p", "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
            "--tools", "Read,Write,Edit,MultiEdit,Grep,Glob,Bash",
            "--permission-mode", "acceptEdits",
            "--settings", dev_settings()]


def _kill_tree(proc):
    """claudeとその子プロセス（ツール実行のsubprocess）ごと確実に止める。
    リーダーだけkillすると、devbot再起動時などに孤児claudeが同じworktreeを
    書き続ける競合が起きうる（start_new_session とセットで使う）。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def stream_claude(prompt, cwd, on_event, *, model=MODEL,
                  timeout=BUILD_TIMEOUT_SEC, idle_timeout=IDLE_TIMEOUT_SEC):
    """claude -p を stream-json で起動し、行ごとに on_event(ev) を呼ぶ（IO）。
    無音が idle_timeout を超えたら中断（ハング保険）。dict(ok/final_text/error/n)。"""
    argv = claude_argv(_claude_bin(), model)
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    assert proc.stdin and proc.stdout and proc.stderr   # PIPE指定で必ず存在
    proc.stdin.write(prompt)
    proc.stdin.close()
    # stderrは別スレッドで吸い続ける（読まずに放置するとパイプ満杯で子が
    # ブロックし、idleタイムアウトの誤発動＝見かけ上のハングになる）
    stderr_chunks = []
    stderr_reader = threading.Thread(
        target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    stderr_reader.start()
    final_text, error, n = "", None, 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:                  # 全体の壁時計超過
                _kill_tree(proc)
                error = f"実装が{timeout}秒を超えたため中断しました"
                break
            wait = min(idle_timeout, remaining)
            rlist, _, _ = select.select([proc.stdout], [], [], wait)
            if not rlist:
                # wait は壁時計残りで頭打ちされるため、超過理由を正しく区別する
                # （残りがidleより短い時のタイムアウトは「無音」ではなく壁時計超過）
                _kill_tree(proc)
                if time.monotonic() >= deadline:
                    error = f"実装が{timeout}秒を超えたため中断しました"
                else:
                    error = f"応答が{idle_timeout}秒無く中断しました"
                break
            line = proc.stdout.readline()
            if line == "":                      # EOF
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            n += 1
            on_event(ev)
            if ev.get("type") == "result":
                res = ev.get("result")
                final_text = res.strip() if isinstance(res, str) else ""
                sub = ev.get("subtype")
                if ev.get("is_error") or (sub and sub != "success"):
                    error = final_text or sub or "詳細不明"
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
    stderr_reader.join(timeout=5)
    if error is None and proc.returncode not in (0, None):
        tail = "".join(c for c in stderr_chunks if c)[-300:]
        error = f"claude exit={proc.returncode}: {tail}"
    return {"ok": error is None, "final_text": final_text, "error": error,
            "n_events": n}


def stage_all(wt):
    """新規ファイルも差分に含めるため全変更をstage（`git diff`は未追跡を見ない）。"""
    _run(["git", "-C", wt, "add", "-A"])


def _merge_base(wt):
    """mainとの共通祖先。resume等でブランチにコミットが積まれていても、
    「表示するdiff」と「mergeで実際に入る内容」を一致させる基準点。"""
    r = _run(["git", "-C", wt, "merge-base", "main", "HEAD"])
    return r.stdout.strip() or "HEAD"


def changed_files(wt):
    """mergeでmainに入る変更の全ファイル（コミット済み＋ステージ済み）。
    -z でクォート表記を避ける（非ASCIIパス対応）。"""
    out = _run(["git", "-C", wt, "diff", "--name-only", "-z",
                _merge_base(wt)]).stdout
    return [f for f in out.split("\0") if f.strip()]


def diff_stat(wt):
    return _run(["git", "-C", wt, "diff", "--stat", _merge_base(wt)]).stdout


def full_diff(wt):
    return _run(["git", "-C", wt, "diff", _merge_base(wt)]).stdout


# gitignoreされたconfig.jsonを持つディレクトリ（worktreeへliveのconfigをsymlink）
_CONFIG_RELS = (ARCHIVE_REL, MEETING_REL)


def ensure_worktree_configs(wt_root):
    """worktreeにはgitignoreのconfig.jsonが無くimportがこけるため、liveのconfigを
    一時的にsymlinkする（テスト直前に呼ぶ）。作成したパスのリストを返す＝
    **呼び出し側がテスト後に必ず削除する**（resume時のclaudeが秘密入りworktreeで
    走るのを防ぐ。worktreeは秘密レスが大前提）。live実行ではdst既存のためno-op。"""
    created = []
    for rel in _CONFIG_RELS:
        live = os.path.join(REPO_ROOT, rel, "config.json")
        dst = os.path.join(wt_root, rel, "config.json")
        if (os.path.exists(dst) or not os.path.exists(live)
                or not os.path.isdir(os.path.dirname(dst))):
            continue
        try:
            os.symlink(live, dst)
        except OSError:
            shutil.copyfile(live, dst)
        created.append(dst)
    return created


#: パスの接頭辞 → そのコードを import しているプロセス。
#: **長い接頭辞から先に見る**（platforms/discord/dev は platforms/discord より優先）
_AREAS = (
    # core は全BOTが import する共有基盤
    ("core/", ("archivebot", "devbot", "meetingbot")),
    ("platforms/discord/dev/", ("devbot",)),
    ("platforms/discord/meeting/", ("meetingbot",)),
    ("platforms/discord/", ("archivebot",)),
    ("integrations/", ("archivebot",)),
    # ダッシュボードは別プロセス。ここでは面倒を見ない
    ("dashboard/", ()),
    ("builder/", ()),
)


def _areas_touched(files):
    """変更ファイルが属する領域の接頭辞集合（純粋関数）。"""
    hit = set()
    for f in files or []:
        for prefix, _ in _AREAS:
            if f.startswith(prefix):
                hit.add(prefix)
                break
    return hit


def _top_dirs(files):
    """リポジトリ直下のどのディレクトリに触れたか（純粋関数）。

    パスはリポジトリルート相対で渡ってくる（例 "core/db.py"）。
    ルート直下のファイル（README.md 等）はどのスイートにも属さない。"""
    return {f.split("/", 1)[0] for f in files or [] if "/" in f}


def suites_for(files):
    """変更ファイル（リポジトリroot相対）→回すべきテストスイート（純粋関数）。

    core は全BOTの土台なので常に回す。platforms は変更があった時だけ足す
    （無関係なスイートの緑で「検証済み」にしない）。"""
    suites = {"core"}
    suites |= _top_dirs(files) & {"platforms"}
    return sorted(suites)


def restart_targets(files):
    """変更ファイル→デプロイ後に再起動すべきBOT（純粋関数）。

    - dev-guidelines.md はジョブごとに読み直すデータ＝再起動不要
    - test_*.py はどのプロセスもimportしない＝再起動不要
    - core/ は全BOTが自プロセスにimportしているので全員が対象
      （片方だけ再起動すると、新旧のコードが混ざった状態になる）
    """
    effective = [f for f in files or []
                 if os.path.basename(f) != "dev-guidelines.md"
                 and not os.path.basename(f).startswith("test_")]
    targets = set()
    for prefix in _areas_touched(effective):
        targets.update(dict(_AREAS)[prefix])
    return sorted(targets)


# 依存関係の変更を示すファイル名（venvへのinstallはパイプラインが面倒を見ない）
_DEPS_HINTS = ("requirements", "pyproject.toml", "setup.py", "setup.cfg")


# テストスイートを持つ（＝検証が効く）ディレクトリ
_TESTED_DIRS = {"core", "platforms"}


def risk_warnings(files):
    """diffの内容から、承認者(👍)に見せる注意書きを作る（純粋関数）。"""
    warns = []
    targets = restart_targets(files)
    areas = _areas_touched(files)
    if "platforms/discord/dev/" in areas and "devbot" in targets:
        warns.append("⚠️ **開発BOT自身（あたしの脳）のコードに触れる変更**っす。"
                     "diffは特に注意して見てほしいっす。反映時はあたしの再起動も入るっす")
    elif any(os.path.basename(f) == "dev-guidelines.md" for f in files or []):
        warns.append("📏 実装規約(dev-guidelines)の変更っす。"
                     "今後の全ジョブの振る舞いに効くやつっす（再起動は不要）")
    elif "devbot" in targets:
        warns.append("🔁 共有モジュール（core/）の変更なので、"
                     "反映時にあたし自身の再起動も入るっす")
    untested = _top_dirs(files) - _TESTED_DIRS
    if untested:
        warns.append(f"🧪 {'、'.join(sorted(untested))} にはテストスイートが"
                     "無いので、自動検証は効いてないっす（目視必須）")
    if any(any(h in os.path.basename(f) for h in _DEPS_HINTS)
           for f in files or []):
        warns.append("📦 依存関係（requirements等）に触れる変更っす。venvへの"
                     "installはパイプラインが面倒見ないので、承認前に中身と"
                     "反映手順を確認してほしいっす")
    if "meetingbot" in targets:
        warns.append("🎙 反映時に meetingbot の再起動が入るっす。"
                     "**会議の録音中でないこと**を確認してから👍してほしいっす")
    return warns


def _suite_python(suite):
    """スイートを回す python。いまは全スイートが同じ venv を使う。"""
    return LIVE_VENV_PY


def run_tests(wt_root, files):
    """変更に応じたスイートを全部回して集約する（IO）。(ok, tail) を返す。
    wt_root は worktree ルート（deployのlive回帰では REPO_ROOT を渡す）。
    テスト用にsymlinkしたconfigは finally で必ず消す（worktreeを秘密レスに保つ）。"""
    created = ensure_worktree_configs(wt_root)
    try:
        ok_all, parts = True, []
        for suite in suites_for(files):
            d = os.path.join(wt_root, suite)
            r = _run([_suite_python(suite), "-m", "unittest", "discover", "-q"],
                     cwd=d, timeout=600)
            ok = r.returncode == 0
            ok_all = ok_all and ok
            out = (r.stderr or r.stdout or "").strip()
            last = out.splitlines()[-1] if out else ""
            parts.append(f"[{suite}] {last}" if ok
                         else f"[{suite}] NG\n{out[-500:]}")
        return ok_all, "\n".join(parts)
    finally:
        for p in created:
            try:
                os.remove(p)
            except OSError:
                pass


def run_pyflakes(root, files):
    """変更した .py に pyflakes（未定義名・未使用importの検出）。
    files は changed_files() のリポジトリroot相対パスなので、root=worktreeルートを
    cwd にして解決する（openagentsディレクトリを渡すと No such file になる）。"""
    pys = [f for f in files if f.endswith(".py")]
    if not pys:
        return True, ""
    r = _run([LIVE_VENV_PY, "-m", "pyflakes", *pys], cwd=root, timeout=120)
    return r.returncode == 0, (r.stdout or r.stderr)
