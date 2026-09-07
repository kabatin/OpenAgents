#!/usr/bin/env python3
"""archive-tools の stdio MCP サーバ。claude -p が --mcp-config 経由で起動する。

依存ゼロの手書き JSON-RPC（initialize / tools/list / tools/call / ping）。
1起動1プロセス・文脈はコマンドライン引数（context.ToolContext）。
ログは stderr（stdout は JSON-RPC 専用）。

起動: ./venv/bin/python -m core.archive_tools.server --agent … --actor …
（launch.build がこの形で --mcp-config を組み立てる。cwd に依存しない）"""

import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.archive_tools import registry  # noqa: E402
from core.archive_tools.context import ToolContext  # noqa: E402

registry.ensure_loaded()

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "archive", "version": "1"}


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def handle(ctx, req, tools):
    """1リクエスト→1レスポンス（通知は None）。純粋関数・テスト対象。"""
    if not isinstance(req, dict):
        return _error(None, -32600, "invalid request")
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return _result(mid, {
            "protocolVersion": params.get("protocolVersion")
            or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": registry.to_mcp(tools)})
    if method == "tools/call":
        res = registry.dispatch(ctx, params.get("name"),
                                params.get("arguments") or {}, tools)
        return _result(mid, {
            "content": [{"type": "text",
                         "text": json.dumps(res, ensure_ascii=False)}],
            "isError": not res.get("ok", False)})
    if mid is None:
        return None  # notifications/* は無視
    return _error(mid, -32601, f"method not found: {method}")


def serve(ctx, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    tools = registry.visible_tools(ctx)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            stdout.write(json.dumps(_error(None, -32700, "parse error"))
                         + "\n")
            stdout.flush()
            continue
        resp = handle(ctx, req, tools)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv=None):
    ctx = ToolContext.from_args(sys.argv[1:] if argv is None else argv)
    serve(ctx)


if __name__ == "__main__":
    main()
