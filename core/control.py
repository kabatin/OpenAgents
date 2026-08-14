#!/usr/bin/env python3
"""スーパーバイザの操作窓口（ローカル限定のHTTP）。

ダッシュボードはこれを叩いて状態を見たり再起動したりする。
以前は `launchctl` を直接呼んでいたが、それだと macOS でしか動かない。
BOTの面倒はスーパーバイザが見る、という形にしたので、
ダッシュボードから見た操作方法はどのOSでも同じになる。

## 守っていること

- **127.0.0.1 でしか待ち受けない**。設定でも外向きにはできない
  （ここを外に出すと、誰でもBOTを止められてしまう）
- 状態を変える操作は POST だけ。GET では絶対に何も変えない
  （ブラウザに `<img src=...>` を踏ませるだけで再起動される事故を防ぐ）
- 依存を増やさないため標準ライブラリだけで書く

単体テスト: python -m unittest core.test_supervisor -v
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 待ち受けアドレス。**ここは設定で変えられない**（意図的）
HOST = "127.0.0.1"
DEFAULT_PORT = 8788


def route(method, path):
    """パスを (種類, 引数) に振り分ける（純粋関数・テスト対象）。

    戻り値: ("status", None) / ("restart", "devbot") / ("not_found", None)
            / ("method_not_allowed", None)
    """
    parts = [p for p in path.split("?")[0].split("/") if p]
    if not parts:
        return ("not_found", None)
    head = parts[0]
    if head == "status":
        if method != "GET":
            return ("method_not_allowed", None)
        return ("status", None)
    if head in ("restart", "start", "stop"):
        # 状態を変える操作は POST のみ（GETでの副作用を作らない）
        if method != "POST":
            return ("method_not_allowed", None)
        if len(parts) != 2:
            return ("not_found", None)
        return (head, parts[1])
    if head == "health":
        return ("health", None)
    return ("not_found", None)


class _Handler(BaseHTTPRequestHandler):
    supervisor = None
    server_version = "OpenAgentsSupervisor/1.0"

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        kind, arg = route(method, self.path)
        if kind == "method_not_allowed":
            return self._send(405, {"message": "この操作は POST で行います"})
        if kind == "not_found":
            return self._send(404, {"message": "不明なパスです"})
        if kind == "health":
            return self._send(200, {"ok": True})
        sup = self.supervisor
        if sup is None:
            return self._send(503, {"message": "起動中です"})
        if kind == "status":
            return self._send(200, sup.status())
        try:
            getattr(sup, kind)(arg)
        except KeyError:
            return self._send(404, {"message": f"知らないBOTです: {arg}"})
        except ValueError as e:
            return self._send(400, {"message": str(e)})
        return self._send(200, {"ok": True, "service": arg, "action": kind})

    def do_GET(self):       # noqa: N802 （標準ライブラリの規約）
        self._handle("GET")

    def do_POST(self):      # noqa: N802
        self._handle("POST")

    def log_message(self, fmt, *args):
        """既定のアクセスログは出さない（BOTのログに混ざって読みにくい）。"""


def serve(supervisor, port=DEFAULT_PORT):
    """control API を別スレッドで動かし、HTTPServer を返す。"""
    handler = type("Handler", (_Handler,), {"supervisor": supervisor})
    httpd = ThreadingHTTPServer((HOST, port), handler)
    thread = threading.Thread(target=httpd.serve_forever,
                              name="control-api", daemon=True)
    thread.start()
    return httpd
