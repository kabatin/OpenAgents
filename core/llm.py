#!/usr/bin/env python3
"""どのAIに考えさせるかの切り替え。

回答生成は全部ここを通る。Claude Code / Codex CLI / その他のコマンドライン
AIは、どれも **「標準入力にプロンプトを流すと標準出力に本文が返る」**
という同じ形をしているので、コマンドの並びを設定に持たせるだけで差し替えられる。

    "llm": {
      "provider": "claude",
      "model": "claude-sonnet-5",
      "timeout_sec": 180
    }

## 正直に書いておく制約

**ツールを使う機能（添付ファイルの読解・開発BOTの自己改修・MCP）は
Claude Code 専用です。** 他のCLIにも似た機能はありますが、権限の渡し方も
出力の形も違うので、動くふりをするより無効にする方が誠実だと判断した。

    provider = claude  → 全機能
    それ以外           → 質問応答・要約・観察ループのみ

何が使えないかは `missing_features()` が返し、ダッシュボードに表示される。
黙って無効化しない。

## 自分のCLIを足す

    "providers": {
      "mycli": {
        "command": ["mycli", "--quiet", "--model", "{model}"],
        "label": "My CLI"
      }
    }

`{model}` はモデル名に置き換わる。プロンプトは標準入力から渡される。
プロンプトを引数で受け取る必要があるCLIには対応していない
（長文でコマンドライン長の上限に当たるため、標準入力で統一している）。

単体テスト: python -m unittest core.test_llm -v
"""

import os
import shutil
import subprocess

#: 標準で用意しているプロバイダ。
#: command の "{model}" は実行時にモデル名へ置き換わる。
BUILTIN_PROVIDERS = {
    "claude": {
        "label": "Claude Code",
        "command": ["claude", "-p", "--model", "{model}", "--tools", ""],
        "bin": "claude",
        "default_model": "claude-sonnet-5",
        "install": "https://claude.com/claude-code",
        # ツール実行・セッション継続に対応した専用の経路を持つ
        "rich": True,
    },
    "codex": {
        "label": "Codex CLI",
        "command": ["codex", "exec", "--model", "{model}", "-"],
        "bin": "codex",
        # 既定を空にして --model 自体を付けない。使えるモデル名は契約
        # （ChatGPTアカウント / APIキー）で違い、決め打ちすると 400 で落ちる
        "default_model": "",
        "install": "https://github.com/openai/codex",
        "rich": False,
    },
}

#: Claude Code でしか動かない機能（人間に見せる名前つき）
CLAUDE_ONLY_FEATURES = {
    "attachments": "添付ファイルの読解",
    "web_search": "Web検索",
    "dev_bot": "開発BOTの自己改修",
    "mcp": "MCPサーバー連携",
    "session_resume": "会話セッションの継続",
}

DEFAULT_TIMEOUT_SEC = 180
#: Web検索や長文読解を伴う経路はもっと待つ
LONG_TIMEOUT_SEC = 600


class LLMError(RuntimeError):
    """AIの呼び出しに失敗した。理由は人間に見せる前提で書く。"""


def provider_table(cfg):
    """組み込み＋利用者定義のプロバイダ一覧。利用者定義が優先。"""
    table = {k: dict(v) for k, v in BUILTIN_PROVIDERS.items()}
    for name, spec in ((cfg.get("llm") or {}).get("providers") or {}).items():
        merged = dict(table.get(name) or {})
        merged.update(spec)
        merged.setdefault("label", name)
        merged.setdefault("rich", False)
        table[name] = merged
    return table


def selected(cfg):
    """設定で選ばれているプロバイダ名（既定 claude）。"""
    return (cfg.get("llm") or {}).get("provider") or "claude"


def spec_for(cfg, name=None):
    """プロバイダ定義を引く。未知の名前は分かるように落とす。"""
    table = provider_table(cfg)
    key = name or selected(cfg)
    if key not in table:
        known = "・".join(sorted(table))
        raise LLMError(
            f"設定の llm.provider が不明です: {key}（使えるのは {known}）")
    return table[key]


def model_for(cfg, name=None):
    """使うモデル名。設定に無ければプロバイダの既定。"""
    explicit = (cfg.get("llm") or {}).get("model")
    if explicit:
        return explicit
    return spec_for(cfg, name).get("default_model") or ""


def timeout_for(cfg, default=DEFAULT_TIMEOUT_SEC):
    return int((cfg.get("llm") or {}).get("timeout_sec") or default)


def find_binary(spec):
    """実行ファイルの場所（見つからなければ None）。

    PATH が最小限な環境（launchd 等）から起動されることがあるので、
    よくある置き場も見る。
    """
    name = spec.get("bin") or (spec.get("command") or [""])[0]
    if not name:
        return None
    found = shutil.which(name)
    if found:
        return found
    for base in ("~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin"):
        candidate = os.path.expanduser(os.path.join(base, name))
        if os.path.exists(candidate):
            return candidate
    return None


def detect_available(cfg=None):
    """インストール済みのプロバイダを調べる（セットアップ画面が使う）。

    戻り値: [{"name","label","installed","path","install","rich"}]
    """
    table = provider_table(cfg or {})
    out = []
    for name, spec in sorted(table.items()):
        path = find_binary(spec)
        out.append({
            "name": name,
            "label": spec.get("label", name),
            "installed": path is not None,
            "path": path or "",
            "install": spec.get("install", ""),
            "rich": bool(spec.get("rich")),
            "default_model": spec.get("default_model", ""),
        })
    return out


def supports_tools(cfg, name=None):
    """ツールを使う機能が動くプロバイダか。"""
    return bool(spec_for(cfg, name).get("rich"))


def missing_features(cfg, name=None):
    """このプロバイダでは使えない機能の一覧（人間に見せる名前）。"""
    if supports_tools(cfg, name):
        return []
    return list(CLAUDE_ONLY_FEATURES.values())


def describe_limits(cfg, name=None):
    """「このプロバイダでは○○が使えません」の1文（問題なければ空文字）。"""
    missing = missing_features(cfg, name)
    if not missing:
        return ""
    label = spec_for(cfg, name).get("label", selected(cfg))
    return f"{label} では次の機能が使えません: " + "・".join(missing)


#: モデル名を渡すためのフラグ（モデル未指定なら、この対ごと落とす）
_MODEL_FLAGS = ("--model", "-m")


def build_argv(spec, model):
    """実行するコマンドの並びを組み立てる（純粋関数・テスト対象）。

    モデル名が空のときは `--model {model}` を**フラグごと**取り除く。
    空文字を渡すとCLI側が「不正なモデル名」として落ちるし、契約の種類に
    よって使えるモデル名が違うツールでは、決め打ちより既定に任せる方が確実。
    """
    command = spec.get("command")
    if not command:
        raise LLMError(
            f"プロバイダ {spec.get('label')} に command が定義されていません")
    binary = find_binary(spec)
    if binary is None:
        name = spec.get("bin") or command[0]
        install = spec.get("install")
        hint = f"\nインストール: {install}" if install else ""
        raise LLMError(f"{name} が見つかりません。PATH を確認してください。{hint}")

    argv = [binary]
    rest = list(command[1:])
    i = 0
    while i < len(rest):
        arg = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else None
        if not model and arg in _MODEL_FLAGS and nxt == "{model}":
            i += 2      # フラグと値をまとめて飛ばす
            continue
        argv.append(arg.replace("{model}", model or ""))
        i += 1
    return argv


def generate(prompt, cfg, *, timeout=None, provider=None):
    """プロンプトを渡して本文を受け取る（テキスト生成のみ）。

    ツールは使わない。添付の読解など、ツールが要る処理は
    `core.invoke_claude` の経路（Claude Code 専用）を通す。
    """
    spec = spec_for(cfg, provider)
    argv = build_argv(spec, model_for(cfg, provider))
    try:
        proc = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            timeout=timeout or timeout_for(cfg),
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError(
            f"{spec.get('label')} が {e.timeout} 秒以内に応答しませんでした"
        ) from e
    except OSError as e:
        raise LLMError(f"{spec.get('label')} を起動できませんでした: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:500]
        raise LLMError(
            f"{spec.get('label')} が失敗しました (exit={proc.returncode})"
            + (f": {detail}" if detail else ""))
    reply = (proc.stdout or "").strip()
    if not reply:
        raise LLMError(f"{spec.get('label')} の出力が空でした")
    return reply
