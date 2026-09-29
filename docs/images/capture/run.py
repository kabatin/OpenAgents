#!/usr/bin/env python3
"""README 用のスクリーンショットを撮り直す（docs/images/*.png を上書きする）。

    python docs/images/capture/run.py

やること（すべて一時ディレクトリの中で完結し、本物の state/ と config.json には触れない）:
  1. リポジトリを一時ディレクトリへコピーし、架空のデモ設定とデモデータを作る
  2. 「全BOT稼働中」を返す偽のスーパーバイザを立てる（run.py の代わり）
  3. 管理画面を組み立てて空いているポートで起動し、ヘッドレス Chrome で撮る
  4. 中身の外接矩形で切り出して保存し、立てたものは自分の PID だけ止める

必要なもの: dashboard/node_modules（npm ci 済み）、Google Chrome、リポジトリの venv（Pillow）。
"""

import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
OUT_DIR = os.path.normpath(os.path.join(HERE, ".."))
CHROME = os.environ.get(
    "CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SKIP = {".git", "venv", "node_modules", "state", "dist", ".claude", "__pycache__",
        ".playwright-mcp", "config.json"}
MAX_KB = 350

AGENTS = [
    ("akari", "あかり", "総務・なんでも相談", "general", "assistant", True),
    ("sora", "そら", "デザインの相談役", "design", "energetic", False),
    ("minato", "みなと", "マーケの壁打ち相手", "marketing", "analyst", False),
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def copy_repo(dst):
    def ignore(_d, names):
        return [n for n in names if n in SKIP]
    shutil.copytree(REPO, dst, ignore=ignore, symlinks=True)
    # worktree などで node_modules が無いときは OA_NODE_MODULES で場所を指定する
    src = os.environ.get("OA_NODE_MODULES",
                         os.path.join(REPO, "dashboard", "node_modules"))
    os.symlink(src, os.path.join(dst, "dashboard", "node_modules"))


def write_config(root):
    sys.path.insert(0, HERE)
    from seed import CH, GUILD  # noqa: E402  （ID の定義をデモデータと揃える）
    cfg = json.load(open(os.path.join(root, "config.example.json"), encoding="utf-8"))
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    cfg["guild_id"] = GUILD
    agents = []
    for aid, name, role, ch, tmpl, archiver in AGENTS:
        persona = f"personas/{aid}.md"
        text = open(os.path.join(root, "personas", f"{tmpl}.template.md"),
                    encoding="utf-8").read()
        body = text.split("---", 2)[2].strip() if text.startswith("---") else text
        body = body.replace("{{AGENT_NAME}}", name).replace("{{TEAM_NAME}}", "サンプル商事")
        open(os.path.join(root, persona), "w", encoding="utf-8").write(body + "\n")
        agents.append({
            "id": aid, "name": name, "token": "demo-token-not-real",
            "home_channel_id": CH[ch], "archiver": archiver,
            "persona_files": [persona], "role": role,
            "skills": {"reminder": True, "youtube_summary": True, "pdf_summary": True},
            "proactive": {"enabled": True, "interval_min": 30, "daily_quota": 3,
                          "rescue": {"enabled": True, "shadow": False}},
            "tool_loop": {"enabled": aid == "akari", "shadow": False},
        })
    cfg["agents"] = agents
    cfg["dev_bot"] = {"enabled": True, "token": "demo-token-not-real",
                      "dev_channel_id": str(CH["general"])}
    cfg["meeting_bot"] = {"enabled": True, "token": "demo-token-not-real",
                          "voice_channel_id": "", "user_mapping": {}}
    path = os.path.join(root, "config.json")
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 「設定を変えたのに再起動していない」判定を出さないよう、起動より前の更新にする
    old = time.time() - 86400
    os.utime(path, (old, old))


def fake_supervisor(root, port):
    """GET /status に「全サービス稼働中」を返す（dashboard/server/ops/probes.ts の形）。"""
    logs = os.path.join(root, "state", "logs")
    os.makedirs(logs, exist_ok=True)
    services = [("archivebot", "会話エージェント"), ("devbot", "開発BOT"),
                ("meetingbot", "議事録BOT")]
    sample = {
        "archivebot": ["[akari] logged in as あかり", "[sora] logged in as そら",
                       "[minato] logged in as みなと", "[akari] 観察ループ開始（30分ごと）",
                       "[akari] rescue: #ai相談室 の未回答1件に回答",
                       "[sora] ripple: 決定の食い違いを1件指摘"],
        "devbot": ["開発BOT 起動", "監視対象 3件を確認", "起票の拾い上げ: 新規なし"],
        "meetingbot": ["議事録BOT 起動", "ボイスチャンネル待機中"],
    }
    for sid, _ in services:
        stamp = time.strftime("%m-%d %H:%M:%S")
        open(os.path.join(logs, f"{sid}.log"), "w", encoding="utf-8").write(
            "".join(f"{stamp} {line}\n" for line in sample[sid]))

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            now = time.time()
            for sid, _ in services:   # ログの鮮度も「動いている」に保つ
                os.utime(os.path.join(logs, f"{sid}.log"), (now, now))
            body = {"services": [{
                "id": sid, "label": label, "enabled": True, "state": "running",
                "pid": 40000 + i, "restarts": 0, "failures": 0,
                "uptimeSec": 3 * 3600 + 120 * i, "lastExit": None,
                "heartbeatAgeSec": 20 if sid == "archivebot" else None,
                "staleAfterSec": 300 if sid == "archivebot" else 0,
                "logPath": os.path.join(logs, f"{sid}.log"),
            } for i, (sid, label) in enumerate(services)]}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_http(url, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.read()
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"起動しませんでした: {url}")


def optimize(path):
    from PIL import Image
    if os.path.getsize(path) <= MAX_KB * 1024:
        Image.open(path).save(path, optimize=True)
        return
    im = Image.open(path).convert("RGB")
    im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) \
      .save(path, optimize=True)


def main():
    tmp = tempfile.mkdtemp(prefix="oa-shots-")
    root = os.path.join(tmp, "repo")
    procs, srv = [], None
    try:
        copy_repo(root)
        write_config(root)
        subprocess.run([sys.executable, os.path.join(HERE, "seed.py"), root], check=True)
        sup_port, dash_port, cdp_port = free_port(), free_port(), free_port()
        srv = fake_supervisor(root, sup_port)
        dash = os.path.join(root, "dashboard")
        subprocess.run(["npm", "run", "build"], cwd=dash, check=True,
                       stdout=subprocess.DEVNULL)
        env = dict(os.environ, DASHBOARD_PORT=str(dash_port),
                   DASHBOARD_HOST="127.0.0.1", OPENAGENTS_SUPERVISOR_PORT=str(sup_port))
        procs.append(subprocess.Popen(
            [os.path.join(dash, "node_modules", ".bin", "tsx"), "server/index.ts"],
            cwd=dash, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
        base = f"http://127.0.0.1:{dash_port}"
        wait_http(base + "/")
        procs.append(subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={cdp_port}",
             f"--user-data-dir={os.path.join(tmp, 'chrome')}", "--no-first-run",
             "--hide-scrollbars", "--lang=ja-JP", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
        ws = json.loads(wait_http(f"http://127.0.0.1:{cdp_port}/json/version"))[
            "webSocketDebuggerUrl"]
        subprocess.run(["node", os.path.join(HERE, "capture.mjs"), ws, base, OUT_DIR],
                       check=True)
        for name in ("setup-wizard", "overview", "settings", "personas", "ops", "data"):
            optimize(os.path.join(OUT_DIR, f"{name}.png"))
    finally:
        for p in procs:   # 自分が立てたプロセスグループだけを止める（名前では探さない）
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
