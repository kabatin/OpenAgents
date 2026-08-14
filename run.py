#!/usr/bin/env python3
"""OpenAgents の常駐プロセス。これ1本を起動すれば全部動く。

    python run.py

有効になっているBOTを子プロセスとして起動し、落ちたら再起動し、
ログをまとめ、ダッシュボードからの操作を受け付ける。
mac でも Windows でも同じように動く。

ログイン時に自動で立ち上げたい場合は `autostart/` を見てください
（OS側に登録するのは**この1本だけ**です）。

止めるときは Ctrl-C。子プロセスも一緒に止まります。
"""

import os
import signal
import sys
import threading

# リポジトリのルートを import 経路に入れる（どこから実行されても動くように）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows のコンソールは既定が cp932/cp1252 で、日本語の出力が
# UnicodeEncodeError になる。落ちるくらいなら文字化け（replace）の方がまし
if os.name == "nt":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

from core import config as app_config     # noqa: E402
from core import control                  # noqa: E402
from core import paths                    # noqa: E402
from core import supervisor as sup        # noqa: E402


def _print_summary(supervisor, port):
    print("=" * 60)
    print("OpenAgents を起動しました")
    print("=" * 60)
    for runner in supervisor.runners.values():
        svc = runner.service
        mark = "起動" if svc.enabled else "オフ"
        print(f"  [{mark}] {svc.label}"
              + (f"  — {svc.note}" if svc.note else ""))
    print()
    print(f"  ログ         : {paths.LOGS_DIR}")
    print(f"  操作用API    : http://{control.HOST}:{port}/status")
    print()
    print("  止めるときは Ctrl-C")
    print("=" * 60, flush=True)


def main():
    paths.ensure_state_dirs()

    try:
        cfg = app_config.load()
    except app_config.ConfigError as e:
        print(f"設定を読めませんでした: {e}")
        return 1

    # 設定が未完成でも**終了しない**。ここで死ぬと、セットアップ画面の
    # 「起動」ボタンに応える相手が居なくなる（実際にそうなっていた）。
    # BOTは起動できないが、操作用APIは開けておき、設定が済んだら
    # ダッシュボードからの要求で起こす。
    problems = app_config.validate(cfg)
    if problems:
        print("設定がまだ完成していないため、BOTは起動せずに待機します:")
        for p in problems:
            print(f"  - {p}")
        print("  （設定が済むと、ダッシュボードから起動できます）\n")

    port = int((cfg.get("supervisor") or {}).get("port")
               or control.DEFAULT_PORT)
    supervisor = sup.Supervisor(cfg)

    try:
        httpd = control.serve(supervisor, port)
    except OSError as e:
        print(f"操作用APIのポート {port} を使えませんでした: {e}")
        print("  すでに run.py が動いていませんか？")
        return 1

    supervisor.start_all()
    _print_summary(supervisor, port)

    stopping = threading.Event()

    def _shutdown(signum, frame):     # noqa: ARG001
        if stopping.is_set():
            return
        stopping.set()
        print("\n停止しています…", flush=True)

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        stopping.wait()
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.stop_all()
        httpd.shutdown()
        print("停止しました。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
