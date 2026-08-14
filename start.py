#!/usr/bin/env python3
"""OpenAgents をはじめる。最初に打つのはこれ1つだけ。

    python start.py

やること:
  1. Python と Node があるか確かめる（無ければ入手先を出して止まる）
  2. 必要なものを入れる（初回だけ。2回目からは飛ばす）
  3. 管理画面を起動して、ブラウザを開く

**ここでは何も質問しません。** トークンも、AIの選択も、性格も、
全部ブラウザの画面で設定します。黒い画面で対話するより、
選択肢が見えている方がずっと分かりやすいからです。

止めるときは Ctrl-C。
"""

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Windows のコンソールは既定が cp932/cp1252 で、日本語の出力が
# UnicodeEncodeError になる。落ちるくらいなら文字化け（replace）の方がまし
if os.name == "nt":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

VENV_DIR = os.path.join(HERE, "venv")
DASHBOARD_DIR = os.path.join(HERE, "dashboard")
DIST_DIR = os.path.join(DASHBOARD_DIR, "dist")

MIN_PYTHON = (3, 10)
MIN_NODE = 20

IS_WINDOWS = platform.system() == "Windows"


# --- 画面表示 ---------------------------------------------------------------

def say(message=""):
    print(message, flush=True)


def step(number, total, message):
    say(f"[{number}/{total}] {message}")


def fail(message, how_to_fix=""):
    say()
    say(f"❌ {message}")
    if how_to_fix:
        say()
        say(how_to_fix)
    say()
    sys.exit(1)


# --- 前提の確認 -------------------------------------------------------------

def check_python():
    if sys.version_info < MIN_PYTHON:
        fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です"
            f"（いまは {sys.version.split()[0]}）",
            "  https://www.python.org/downloads/ から新しい Python を入れてください。",
        )


def node_version():
    node = shutil.which("node")
    if node is None:
        return None, None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return node, None
    try:
        return node, int(out.lstrip("v").split(".")[0])
    except (ValueError, IndexError):
        return node, None


def check_node():
    node, major = node_version()
    if node is None:
        fail(
            "Node.js が見つかりません（管理画面を動かすのに必要です）",
            "  https://nodejs.org/ から LTS 版を入れて、ターミナルを開き直してください。",
        )
    if major is not None and major < MIN_NODE:
        fail(
            f"Node.js {MIN_NODE} 以上が必要です（いまは v{major}）",
            "  https://nodejs.org/ から LTS 版を入れ直してください。",
        )


# --- 準備 -------------------------------------------------------------------

def venv_python():
    candidates = (
        os.path.join(VENV_DIR, "Scripts", "python.exe"),
        os.path.join(VENV_DIR, "bin", "python"),
    )
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


#: 準備の全ログを残す場所。「失敗しました」だけ見せて原因を隠さない
SETUP_LOG = os.path.join(HERE, "state", "logs", "setup.log")


def _append_log(text):
    try:
        os.makedirs(os.path.dirname(SETUP_LOG), exist_ok=True)
        with open(SETUP_LOG, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _hint_for(detail):
    """よくある失敗に、具体的な直し方を添える。

    決め打ちにしない — 同じ「gyp」の失敗でも原因は複数ある
    （Nodeが新しすぎて部品が未対応・コンパイラ不足など）。
    可能性を確度の高い順に並べて見せる。
    """
    if "gyp" in detail or "node-gyp" in detail:
        return ("  部品のコンパイルに失敗しています。よくある原因の順に:\n"
                "  1. リポジトリが古い → git pull してから、もう一度実行\n"
                "     （新しいNodeに対応した部品構成に更新済みです）\n"
                "  2. mac でコンパイラが無い → xcode-select --install\n"
                "  3. それでも駄目なら、Node の LTS版（22/24）でお試しください:\n"
                "     https://nodejs.org/")
    if "EAI_AGAIN" in detail or "ETIMEDOUT" in detail or "ENOTFOUND" in detail:
        return "  ネットワークに繋がっていないか、プロキシの設定が必要かもしれません。"
    if "EACCES" in detail or "Permission denied" in detail:
        return "  フォルダの書き込み権限がありません。所有者を確認してください。"
    return ""


def run(argv, *, cwd=None, what=""):
    """外部コマンドを実行。失敗したら**理由と直し方**を見せて止める。

    出力の全文は state/logs/setup.log に残す。画面には末尾だけ出すので、
    それで足りないときはログを見ればよい。
    """
    _append_log(f"\n$ {' '.join(argv)}\n")
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as e:
        fail(f"{what}に失敗しました: {e}")
        return
    _append_log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        tail = "  " + "\n  ".join(detail.splitlines()[-15:]) if detail else \
               "  （出力がありません）"
        hint = _hint_for(detail)
        fail(f"{what}に失敗しました",
             tail
             + (f"\n\n{hint}" if hint else "")
             + f"\n\n  全ログ: {SETUP_LOG}")


def ensure_venv():
    python = venv_python()
    if python is not None:
        return python
    say("    Python の作業環境を作っています…")
    run([sys.executable, "-m", "venv", VENV_DIR], what="作業環境の作成")
    python = venv_python()
    if python is None:
        fail("作業環境を作れませんでした",
             "  venv/ を消してから、もう一度 python start.py を実行してください。")
    return python


def _requirements_stamp():
    """requirements.txt の中身から作る印。中身が変われば印も変わる。"""
    import hashlib
    try:
        with open(os.path.join(HERE, "requirements.txt"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def ensure_python_deps(python):
    """必要なものを入れる。

    「discord が import できたら導入済み」では、**requirements.txt に
    後から足された依存を取りこぼす**（更新した既存利用者が壊れる）。
    中身のハッシュを控えておき、変わったときだけ入れ直す。
    """
    stamp_file = os.path.join(VENV_DIR, ".requirements-stamp")
    want = _requirements_stamp()
    try:
        with open(stamp_file, encoding="utf-8") as f:
            if f.read().strip() == want and want:
                return
    except OSError:
        pass
    say("    必要なものを入れています（初回は数分かかります）…")
    run([python, "-m", "pip", "install", "--upgrade", "pip"],
        what="pip の更新")
    run([python, "-m", "pip", "install", "-r",
         os.path.join(HERE, "requirements.txt")],
        what="Python パッケージの導入")
    try:
        with open(stamp_file, "w", encoding="utf-8") as f:
            f.write(want)
    except OSError:
        pass   # 印を残せなくても動作には影響しない（次回また入れ直すだけ）


def _newest_mtime(root, exts):
    """root 以下で、その拡張子を持つファイルの最終更新時刻の最大値。"""
    newest = 0.0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "dist", ".git")]
        for name in files:
            if name.endswith(exts):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(base, name)))
                except OSError:
                    pass
    return newest


def _dashboard_needs_build():
    """管理画面を組み立て直す必要があるか。

    **「dist があるから省略」では駄目**。git pull で画面のコードが新しく
    なっても、古い組み立て結果が配信され続けて修正が届かない
    （実際にそうなった）。ソースの方が新しければ作り直す。
    """
    index = os.path.join(DIST_DIR, "index.html")
    if not os.path.exists(index):
        return True
    try:
        built_at = os.path.getmtime(index)
    except OSError:
        return True
    sources = _newest_mtime(DASHBOARD_DIR, (".ts", ".tsx", ".css", ".html", ".json"))
    return sources > built_at


def ensure_dashboard():
    node_modules = os.path.join(DASHBOARD_DIR, "node_modules")
    npm = shutil.which("npm") or ("npm.cmd" if IS_WINDOWS else None)
    if npm is None:
        fail("npm が見つかりません",
             "  Node.js を入れ直すと一緒に入ります: https://nodejs.org/")
    if not os.path.isdir(node_modules):
        say("    管理画面の部品を入れています（初回は数分かかります）…")
        # --silent は使わない。エラーの中身まで抑制されて、失敗時に
        # 「失敗しました」しか分からなくなる（実際に起きた）
        run([npm, "ci", "--no-audit", "--no-fund"], cwd=DASHBOARD_DIR,
            what="管理画面の準備")
    if _dashboard_needs_build():
        say("    管理画面を組み立てています…")
        run([npm, "run", "build"], cwd=DASHBOARD_DIR, what="管理画面の組み立て")


# --- 起動 -------------------------------------------------------------------

def dashboard_url():
    """設定に書かれたポートで開く（既定 8787）。"""
    port = 8787
    try:
        from core import config as app_config
        port = int((app_config.load().get("dashboard") or {}).get("port") or 8787)
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


def wait_until_up(url, timeout=60):
    """起動を待つ。開く前に確かめないと、真っ白なページを見せてしまう。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def open_browser_when_ready(url):
    def worker():
        if wait_until_up(url):
            say()
            say("  ブラウザを開きます。開かない場合は、次のURLを手で開いてください:")
            say(f"    {url}")
            say()
            try:
                webbrowser.open(url)
            except Exception:
                pass
        else:
            say(f"  管理画面の起動を確認できませんでした。{url} を手で開いてみてください。")

    threading.Thread(target=worker, daemon=True).start()


def start_supervisor(python):
    """BOTたちの親（run.py）を裏で動かす。

    設定がまだ無ければ run.py は「設定が足りません」と言って終わるので、
    ここでは起動を試みるだけでよい。設定が済んだあとにダッシュボードから
    「起動」を押すと、この親プロセスがBOTを立ち上げる。
    """
    try:
        return subprocess.Popen(
            [python, os.path.join(HERE, "run.py")],
            cwd=HERE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        say(f"    常駐プロセスを起動できませんでした: {e}")
        return None


def main():
    say("=" * 60)
    say("  OpenAgents をはじめます")
    say("=" * 60)

    step(1, 3, "動かすのに必要なものを確認しています")
    check_python()
    check_node()

    step(2, 3, "準備をしています")
    python = ensure_venv()
    ensure_python_deps(python)
    ensure_dashboard()

    step(3, 3, "起動しています")
    url = dashboard_url()
    open_browser_when_ready(url)

    # BOTの親プロセスも一緒に動かす。これが無いと、設定を終えても
    # 「起動」ボタンが効かない（押す相手が居ない）
    supervisor = start_supervisor(python)

    npm = shutil.which("npm") or "npm"
    env = dict(os.environ)
    env["NODE_ENV"] = "production"
    try:
        subprocess.run([npm, "start"], cwd=DASHBOARD_DIR, env=env)
    except KeyboardInterrupt:
        pass
    finally:
        if supervisor is not None and supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=15)
            except subprocess.TimeoutExpired:
                supervisor.kill()
        say()
        say("  止めました。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        sys.exit(0)
