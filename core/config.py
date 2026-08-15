#!/usr/bin/env python3
"""設定（config.json）の読み込みと検査。

設定はリポジトリ直下の `config.json` 1枚だけ。全BOTとダッシュボードが
これを見る。**設定ファイルが無いことは異常ではない**（初回起動）ので、
読み込みは静かに既定値を返し、「起動してよいか」の判断は `validate()` が担う。
そうしないと、設定を作るための画面すら起動できなくなる。

## 秘密の書き方

トークンは直接書いても、環境変数を参照してもよい。

    "token": "MTIzNDU2..."           ← そのまま
    "token": "${DISCORD_TOKEN_A1}"   ← 環境変数から読む（.env も見る）

環境変数方式にすると config.json を人に見せられる。未定義の変数を
参照していたら**空文字に潰さず**エラーにする（黙って起動しないBOTになるため）。

単体テスト: python -m unittest core.test_config -v
"""

import json
import os
import re

from core import paths

#: ${VAR} 形式の参照
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

#: config.json がまだ無いとき（初回セットアップ前）の姿。
#: この状態でも import は通り、ダッシュボードは開ける。
EMPTY_CONFIG = {
    "guild_id": "",
    "agents": [],
    "admins": [],
    "llm": {},
    "integrations": {"enabled": []},
}


class ConfigError(Exception):
    """設定が読めない・壊れている。起動を止めてよい種類の失敗。"""


def _load_dotenv(path=None):
    """.env を読んで環境変数に入れる（既存の環境変数を上書きしない）。

    python-dotenv を足すほどの処理ではないので自前で読む。
    `KEY=value` の行だけを見て、`#` から始まる行と空行は飛ばす。
    """
    target = path or os.path.join(paths.ROOT, ".env")
    try:
        with open(target, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def expand_env(value, *, strict=True):
    """設定値の中の ${VAR} を環境変数で置き換える（再帰的）。

    strict=True なら未定義の変数で ConfigError。
    strict=False なら参照文字列をそのまま残す（画面表示用）。
    """
    if isinstance(value, dict):
        return {k: expand_env(v, strict=strict) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, strict=strict) for v in value]
    if not isinstance(value, str):
        return value
    m = _ENV_REF.match(value.strip())
    if not m:
        return value
    name = m.group(1)
    if name in os.environ:
        return os.environ[name]
    if strict:
        raise ConfigError(
            f"環境変数 {name} が未設定です（config.json が ${{{name}}} を"
            "参照しています）。.env に書くか、環境変数として設定してください")
    return value


def load(path=None, *, expand=True):
    """config.json を読む。まだ無ければ EMPTY_CONFIG を返す。

    壊れたJSONは既定値に倒さず落とす（設定が消えたように見えるのを避ける）。
    """
    target = path or paths.CONFIG_PATH
    try:
        with open(target, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(EMPTY_CONFIG))   # 毎回新しい辞書を返す
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json のJSONが壊れています: {e}") from e
    except OSError as e:
        raise ConfigError(f"config.json を読めませんでした: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config.json の中身がオブジェクトではありません")
    if expand:
        _load_dotenv()
        return expand_env(raw)
    return raw


def _agent_problems(agents):
    problems = []
    seen = set()
    for i, a in enumerate(agents):
        where = a.get("id") or f"{i + 1}体目"
        if not a.get("id"):
            problems.append(f"{where}: id が未設定です")
        elif a["id"] in seen:
            problems.append(f"エージェントIDが重複しています: {a['id']}")
        else:
            seen.add(a["id"])
        if not a.get("name"):
            problems.append(f"{where}: 表示名（name）が未設定です")
        if not a.get("home_channel_id"):
            problems.append(f"{where}: ホームチャンネルが未設定です")
    archivers = [a for a in agents if a.get("archiver")]
    if len(archivers) != 1:
        problems.append(
            "会話を記録する担当（archiver）はちょうど1体にしてください"
            f"（今は{len(archivers)}体）")
    elif not str(archivers[0].get("token") or "").strip():
        problems.append(
            f"{archivers[0].get('id')}: Botトークンが未設定です")
    return problems


def validate(cfg):
    """BOTを起動してよい設定か検査し、問題のリストを返す（空なら起動可）。

    「起動できない理由」を全部並べる。1つ見つけて即終了すると、直しては
    また別の理由で落ちる、を繰り返させることになる。
    """
    problems = []
    if "discord_token" in cfg and not cfg.get("agents"):
        return ["config.json が古い形式です。agents 配列を使う新しい形式へ"
                "移行してください（docs/02-configuration.md）"]
    agents = cfg.get("agents") or []
    if not agents:
        problems.append("エージェントが1体も登録されていません")
    else:
        problems.extend(_agent_problems(agents))
    if not cfg.get("guild_id"):
        problems.append("Discordサーバー（guild_id）が未設定です")
    dev = cfg.get("dev_bot") or {}
    if dev.get("enabled") and not str(dev.get("token") or "").strip():
        problems.append("開発BOTが有効ですが、Botトークンが未設定です")
    meeting = cfg.get("meeting_bot") or {}
    if meeting.get("enabled"):
        if not str(meeting.get("token") or "").strip():
            problems.append("議事録BOTが有効ですが、Botトークンが未設定です")
        if not meeting.get("voice_channel_name"):
            problems.append("議事録BOTが有効ですが、録音対象の"
                            "ボイスチャンネル名が未設定です")
    return problems


def guild_mismatch_problem(archived_guild_id, configured_guild_id):
    """アーカイブが別サーバーのものになっていないか（純粋関数）。

    会話の記録は guild_id を持たないため、繋ぎ先だけ別サーバーに変えると
    **前のサーバーの会話が消せないまま検索対象に残る**。エージェントが
    他所の会話を引用してしまうので、混ざる前に起動を止める。

    問題があれば人間向けの1文、無ければ None を返す。
    - 未記録（初回・既存の古いDB）→ 問題なし。次の記録時に覚える
    - 設定が空 → validate 側の担当なのでここでは触らない
    """
    stored = str(archived_guild_id or "").strip()
    current = str(configured_guild_id or "").strip()
    if not stored or not current or stored == current:
        return None
    return (
        f"会話の記録は別のDiscordサーバー（{stored}）のものです"
        f"（いまの設定は {current}）。\n"
        "    記録にはサーバーの区別が無いため、このまま起動すると"
        "前のサーバーの会話を引用してしまいます。\n"
        "    ダッシュボードの「危険な操作」から初期化するか、"
        "元のサーバーに設定を戻してください。"
    )


def require_valid(cfg):
    """検査に通らなければ、理由を並べて ConfigError を投げる。"""
    problems = validate(cfg)
    if problems:
        raise ConfigError(
            "設定が足りないため起動できません:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nダッシュボード（python start.py）から設定してください。")


def agent_by_id(cfg, agent_id):
    """エージェント定義を1件引く（無ければ None）。"""
    for a in cfg.get("agents") or []:
        if a.get("id") == agent_id:
            return a
    return None
