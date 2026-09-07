#!/usr/bin/env python3
"""
Claude起動の一箇所隔離（エージェントv2 Phase 0 / v4 Phase 0 計測）。
設計: docs/08-architecture.md「LLM呼び出しの計測」

すべてのbotはこのモジュール経由で claude CLI をヘッドレス起動する。
Discord層・ツール・記憶は起動方法を知らない。

--output-format stream-json --verbose でイベントを1行ずつ解釈する。
既知の落とし穴への対応: -p のプレーン出力はツール実行を挟むと最終メッセージ
しか出力されず、本文が中間メッセージに消える（tasks/lessons.md）。
stream-json なら全 assistant メッセージを取得できるため本文を失わない。

計測（v4 Phase 0）: result イベントの usage / total_cost_usd / duration_ms /
num_turns / permission_denials を InvokeResult.meta に持ち、モジュール変数
RECORDER（callable）が設定されていれば成功・失敗を問わず1起動1回呼ぶ。
このモジュールは DB を知らない（bot.py が RECORDER に db 書込を差す）。
どのエージェントの起動かは contextvar CURRENT_AGENT で伝える
（asyncio.to_thread はコンテキストを複製するので、タスク起点で set すれば届く）。
"""

import contextvars
import json
import subprocess
import sys
import time

from core import config as app_config
from core import llm

DEFAULT_MODEL = llm.BUILTIN_PROVIDERS["claude"]["default_model"]
# Web検索・URL取得を伴う回答は5分を超えることがあるため10分
DEFAULT_TIMEOUT_SEC = llm.LONG_TIMEOUT_SEC

# ホーム配下Readの禁止（プロンプトインジェクション対策の常設deny）。
# 主防御は --permission-mode default（cwd外Readを自動拒否）で、これは
# モード指定が万一効かない場合の保険（deny は常に優先される）。
DENY_RULES = ["Read(~/**)"]

# 固定費対策: user/project の設定ファイルを読まない。
#   - 利用者の CLAUDE.md（対話用ルール）やプラグイン/スキル一覧がシステム
#     プロンプトから消え、1呼び出しあたり約8kトークン減る
#   - 対話用のルールがBOTの回答に漏れ込む事故の根本対策
#   - --settings で渡す allow/deny と --mcp-config はそのまま効く
# 加えて cwd/git 状態などの可変部を先頭ユーザーメッセージへ寄せ、キャッシュの
# 前置きを起動ごとに変えない（--exclude-dynamic-system-prompt-sections）。
SETTING_SOURCES = ""
EXCLUDE_DYNAMIC_SECTIONS = True

# 計測フック（bot.py が設定）。None なら記録しない。例外は握って本流に影響させない
RECORDER = None
# 起動元エージェントid（bot.py の main がクライアントごとのタスク起点で set する）
CURRENT_AGENT = contextvars.ContextVar("invoke_claude_agent", default=None)


def _guard_settings(allow):
    """--settings に渡す権限設定。allow は事前承認するツール
    （例: WebSearch/WebFetch はheadlessのdefaultモードでは既定拒否のため、
    明示allowしないと実行できない）。deny は常に優先される。"""
    return json.dumps({"permissions": {
        "allow": list(allow), "deny": DENY_RULES}})


class InvokeResult:
    """1回の起動結果。生成後に書き換えない。"""

    def __init__(self, events, final_text, assistant_texts, session_id=None,
                 meta=None):
        self.events = events                    # stream-json の全イベント
        self.final_text = final_text            # result イベントの本文
        self.assistant_texts = assistant_texts  # 全 assistant テキストブロック
        self.session_id = session_id            # 会話セッションid（resume用）
        self.meta = meta or {}                  # 計測値（extract_meta 参照）

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


def extract_meta(events):
    """stream-json イベント群から計測値を取り出す（純粋関数・テスト対象）。

    result イベントの usage / total_cost_usd / duration_ms / num_turns /
    stop_reason / permission_denials と、assistant の tool_use 数を集める。
    無い項目は None（0 と区別する: 失敗起動では result が来ない）。
    """
    meta = {
        "duration_ms": None, "duration_api_ms": None, "num_turns": None,
        "cost_usd": None, "input_tokens": None, "output_tokens": None,
        "cache_read_tokens": None, "cache_creation_tokens": None,
        "thinking_tokens": None, "stop_reason": None, "denials": 0,
        "tool_calls": 0, "tools_used": [], "models": [],
    }
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    meta["tool_calls"] += 1
                    name = block.get("name")
                    if name and name not in meta["tools_used"]:
                        meta["tools_used"].append(name)
        elif ev.get("type") == "result":
            usage = ev.get("usage") or {}
            details = usage.get("output_tokens_details") or {}
            meta.update({
                "duration_ms": ev.get("duration_ms"),
                "duration_api_ms": ev.get("duration_api_ms"),
                "num_turns": ev.get("num_turns"),
                "cost_usd": ev.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
                "thinking_tokens": details.get("thinking_tokens"),
                "stop_reason": ev.get("stop_reason"),
                "denials": len(ev.get("permission_denials") or []),
                "models": list((ev.get("modelUsage") or {}).keys()),
            })
    return meta


def build_argv(claude_bin, *, model, system=None, allowed_tools=(),
               allow=(), mcp_config=None, resume=None, max_budget_usd=None):
    """claude CLI の引数列を組み立てる（純粋関数・テスト対象）。
    resume: 継続する会話セッションid（cwdスコープ＝同じcwdでの起動が必要）。
    mcp_config: MCPサーバ設定（JSON文字列 or パス）。指定時は --strict-mcp-config を
        付けてユーザー設定のMCPを混ぜない。組込ツールが空でも settings の allow
        （mcp__サーバ__ツール）を出さないと MCP ツールが全部 denied になる。
    max_budget_usd: 1起動の上限額（ツールループの安全弁）。"""
    argv = [claude_bin, "-p", "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", SETTING_SOURCES]
    if EXCLUDE_DYNAMIC_SECTIONS:
        argv.append("--exclude-dynamic-system-prompt-sections")
    if resume:
        argv += ["--resume", resume]
    if system:
        argv += ["--append-system-prompt", system]
    argv += ["--tools", ",".join(allowed_tools) if allowed_tools else ""]
    if allowed_tools or mcp_config:
        argv += ["--permission-mode", "default",  # cwd外Readを自動拒否
                 "--settings", _guard_settings(allow)]
    if mcp_config:
        argv += ["--mcp-config", mcp_config, "--strict-mcp-config"]
    if max_budget_usd:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv


class _Proc:
    """subprocess.run の戻り値と同じ形（streaming 経路用）。"""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_streaming(argv, prompt, *, timeout, cwd, on_event):
    """stream-json を1行ずつ読み、イベントごとに on_event を呼ぶ起動経路。
    壁時計で kill（TimeoutExpired を送出）。stdin/stderr は別スレッドで捌く
    （大きなプロンプトやエラー出力でパイプが詰まらないように）。"""
    import threading

    proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd)
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    err_buf = []

    def _drain_err():
        err_buf.append(proc.stderr.read())

    def _feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    t_err = threading.Thread(target=_drain_err, daemon=True)
    t_in = threading.Thread(target=_feed, daemon=True)
    t_err.start()
    t_in.start()
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                try:
                    on_event(ev)
                except Exception as e:  # noqa: BLE001 - 進捗通知で本流を止めない
                    print(f"[invoke_claude] on_event failed: {e}",
                          file=sys.stderr)
        proc.wait()
    finally:
        timer.cancel()
        t_err.join(timeout=5)
        t_in.join(timeout=5)
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(argv, timeout)
    return _Proc(proc.returncode, "".join(lines),
                 "".join(err_buf) if err_buf else "")


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


def _record(base, meta=None, *, ok, error=None, started):
    """1起動1回の計測記録。RECORDER 未設定なら何もしない。失敗は本流に影響させない。"""
    if RECORDER is None:
        return
    rec = dict(meta or extract_meta([]))
    rec.update(base)
    rec.update({
        "ok": bool(ok),
        "error": (error or None) and str(error)[:300],
        "wall_ms": int((time.monotonic() - started) * 1000),
        "agent_id": CURRENT_AGENT.get(),
    })
    try:
        RECORDER(rec)
    except Exception as e:  # noqa: BLE001 - 計測の失敗で回答を止めない
        print(f"[invoke_claude] record failed: {e}", file=sys.stderr)


def invoke(prompt, *, model=DEFAULT_MODEL, system=None, allowed_tools=(),
           allow=(), timeout=DEFAULT_TIMEOUT_SEC, cwd=None, mcp_config=None,
           resume=None, purpose="other", on_event=None, max_budget_usd=None):
    """claude CLI を1回ヘッドレス起動して InvokeResult を返す。

    prompt: 本文（ARG_MAX/クォート回避のため stdin 経由で渡す）
    system: ペルソナ＋方針（--append-system-prompt）
    allowed_tools: 有効化するツール名タプル（例: ("Read","WebSearch")）。
        指定時は cwd 閉じ込め＋ホーム配下 Read 禁止のガードを併せて渡す。
        未指定なら --tools "" で全ツール無効（テキスト生成のみ）。
    allow: headlessのdefaultモードで既定拒否されるツールの事前承認リスト
        （例: ("WebSearch","WebFetch")）。Readはcwd内なら自動許可される。
    mcp_config: MCPサーバ設定（JSON文字列 or パス）。ツールループで使用。
        指定時は --strict-mcp-config が付き、渡したサーバだけが見える
    purpose: 計測用の用途ラベル（answer / keywords / screen / decide / summary …）
    on_event: stream-json イベントごとの進捗コールバック（指定時は行読み経路）
    max_budget_usd: 1起動の上限額（--max-budget-usd）
    失敗時は RuntimeError を送出する（計測には失敗として記録される）。
    """
    unavailable = check_available()
    if unavailable:
        raise RuntimeError(unavailable)
    claude_bin = _claude_bin()
    argv = build_argv(claude_bin, model=model, system=system,
                      allowed_tools=allowed_tools, allow=allow,
                      mcp_config=mcp_config, resume=resume,
                      max_budget_usd=max_budget_usd)
    base = {"purpose": purpose, "model": model}
    started = time.monotonic()
    try:
        if on_event is not None:
            proc = _run_streaming(argv, prompt, timeout=timeout, cwd=cwd,
                                  on_event=on_event)
        else:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
    except subprocess.TimeoutExpired:
        _record(base, ok=False, error=f"timeout {timeout}s", started=started)
        raise
    except OSError as e:
        _record(base, ok=False, error=f"起動失敗: {e}", started=started)
        raise
    events, final_text, assistant_texts, error_message = parse_stream_events(
        proc.stdout.splitlines())
    meta = extract_meta(events)
    if proc.returncode != 0:
        err = proc.stderr.strip()[:500]
        _record(base, meta, ok=False,
                error=f"exit={proc.returncode}: {err or error_message or ''}",
                started=started)
        raise RuntimeError(
            f"claude CLI 失敗 (exit={proc.returncode}): {err}"
        )
    if error_message is not None:
        _record(base, meta, ok=False, error=error_message, started=started)
        raise RuntimeError(f"claude 実行がエラー終了: {error_message[:300]}")
    result = InvokeResult(events, final_text, assistant_texts,
                          session_id=extract_session_id(events), meta=meta)
    if not result.text:
        _record(base, meta, ok=False, error="出力が空", started=started)
        raise RuntimeError("claude の出力が空でした")
    _record(base, meta, ok=True, started=started)
    return result
