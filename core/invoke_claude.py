#!/usr/bin/env python3
"""
Claude起動の一箇所隔離（エージェントv2 Phase 0）。設計: docs/agents-v2-design.md §2.2

すべてのbotはこのモジュール経由で claude CLI をヘッドレス起動する。
後で Agent SDK に差し替える時はここだけ変更すればよい
（Discord層・ツール・記憶は不変）。

--output-format stream-json --verbose でイベントを1行ずつ解釈する。
既知の落とし穴への対応: -p のプレーン出力はツール実行を挟むと最終メッセージ
しか出力されず、本文が中間メッセージに消える（tasks/lessons.md）。
stream-json なら全 assistant メッセージを取得できるため本文を失わない。
"""

import json
import os
import subprocess

from core import config as app_config
from core import llm

DEFAULT_MODEL = llm.BUILTIN_PROVIDERS["claude"]["default_model"]
# Web検索・URL取得を伴う回答は5分を超えることがあるため10分
DEFAULT_TIMEOUT_SEC = llm.LONG_TIMEOUT_SEC

# ホーム配下Readの禁止（プロンプトインジェクション対策の常設deny）。
# 主防御は --permission-mode default（cwd外Readを自動拒否）で、これは
# モード指定が万一効かない場合の保険（deny は常に優先される）。
DENY_RULES = ["Read(~/**)"]


def _guard_settings(allow):
    """--settings に渡す権限設定。allow は事前承認するツール
    （例: WebSearch/WebFetch はheadlessのdefaultモードでは既定拒否のため、
    明示allowしないと実行できない）。deny は常に優先される。"""
    return json.dumps({"permissions": {
        "allow": list(allow), "deny": DENY_RULES}})


class InvokeResult:
    """1回の起動結果。生成後に書き換えない。"""

    def __init__(self, events, final_text, assistant_texts, session_id=None):
        self.events = events                    # stream-json の全イベント
        self.final_text = final_text            # result イベントの本文
        self.assistant_texts = assistant_texts  # 全 assistant テキストブロック
        self.session_id = session_id            # 会話セッションid（resume用）

    @property
    def text(self):
        """回答本文。result イベントを正とし、空なら assistant 本文の連結。"""
        return self.final_text or "\n\n".join(self.assistant_texts)


def parse_stream_events(lines):
    """stream-json の行群を解釈する（純粋関数・テスト対象）。

    Returns:
        (events, final_text, assistant_texts, error_message)
        error_message は result イベントがエラーを示す場合のみ非None。
    """
    events = []
    final_text = ""
    assistant_texts = []
    error_message = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # 非JSON行（警告等）は無視
        if not isinstance(ev, dict):
            continue
        events.append(ev)
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = (block.get("text") or "").strip()
                    if t:
                        assistant_texts.append(t)
        elif ev.get("type") == "result":
            res = ev.get("result")
            final_text = res.strip() if isinstance(res, str) else ""
            subtype = ev.get("subtype")
            if ev.get("is_error") or (subtype and subtype != "success"):
                error_message = final_text or subtype or "詳細不明"
    return events, final_text, assistant_texts, error_message


def extract_session_id(events):
    """stream-json イベント群から会話セッションidを取り出す（純粋関数）。"""
    for ev in events:
        sid = ev.get("session_id")
        if sid:
            return sid
    return None


def build_argv(claude_bin, *, model, system=None, allowed_tools=(),
               allow=(), mcp_config=None, resume=None):
    """claude CLI の引数列を組み立てる（純粋関数・テスト対象）。
    resume: 継続する会話セッションid（cwdスコープ＝同じcwdでの起動が必要）。"""
    argv = [claude_bin, "-p", "--model", model,
            "--output-format", "stream-json", "--verbose"]
    if resume:
        argv += ["--resume", resume]
    if system:
        argv += ["--append-system-prompt", system]
    if allowed_tools:
        argv += ["--tools", ",".join(allowed_tools),
                 "--permission-mode", "default",  # cwd外Readを自動拒否
                 "--settings", _guard_settings(allow)]
    else:
        argv += ["--tools", ""]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    return argv


def _claude_bin():
    """claude CLI の場所（PATH が最小限な環境も見る。詳細は core/llm.py）。"""
    return llm.find_binary(llm.BUILTIN_PROVIDERS["claude"])


def check_available(cfg=None):
    """この経路が使える状態か検査し、使えないなら理由を返す（使えれば None）。

    ここはツール実行・セッション継続・MCP を伴う **Claude Code 専用の経路**。
    他のプロバイダを選んでいるときに黙って claude を起動すると、
    利用者が選んだ設定を無視することになるので、理由を出して断る。
    """
    config = cfg if cfg is not None else _config()
    if not llm.supports_tools(config):
        return llm.describe_limits(config)
    if _claude_bin() is None:
        return ("claude CLI が見つかりません。"
                "インストール: https://claude.com/claude-code")
    return None


_CACHED_CONFIG = None


def _config():
    global _CACHED_CONFIG
    if _CACHED_CONFIG is None:
        try:
            _CACHED_CONFIG = app_config.load()
        except app_config.ConfigError:
            _CACHED_CONFIG = {}
    return _CACHED_CONFIG


def invoke(prompt, *, model=DEFAULT_MODEL, system=None, allowed_tools=(),
           allow=(), timeout=DEFAULT_TIMEOUT_SEC, cwd=None, mcp_config=None,
           resume=None):
    """claude CLI を1回ヘッドレス起動して InvokeResult を返す。

    prompt: 本文（ARG_MAX/クォート回避のため stdin 経由で渡す）
    system: ペルソナ＋方針（--append-system-prompt）
    allowed_tools: 有効化するツール名タプル（例: ("Read","WebSearch")）。
        指定時は cwd 閉じ込め＋ホーム配下 Read 禁止のガードを併せて渡す。
        未指定なら --tools "" で全ツール無効（テキスト生成のみ）。
    allow: headlessのdefaultモードで既定拒否されるツールの事前承認リスト
        （例: ("WebSearch","WebFetch")）。Readはcwd内なら自動許可される。
    mcp_config: MCPサーバ設定のパス（Phase 1以降で使用）
    失敗時は RuntimeError を送出する。
    """
    unavailable = check_available()
    if unavailable:
        raise RuntimeError(unavailable)
    claude_bin = _claude_bin()
    argv = build_argv(claude_bin, model=model, system=system,
                      allowed_tools=allowed_tools, allow=allow,
                      mcp_config=mcp_config, resume=resume)
    proc = subprocess.run(
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI 失敗 (exit={proc.returncode}): {proc.stderr.strip()[:500]}"
        )
    events, final_text, assistant_texts, error_message = parse_stream_events(
        proc.stdout.splitlines())
    if error_message is not None:
        raise RuntimeError(f"claude 実行がエラー終了: {error_message[:300]}")
    result = InvokeResult(events, final_text, assistant_texts,
                          session_id=extract_session_id(events))
    if not result.text:
        raise RuntimeError("claude の出力が空でした")
    return result
