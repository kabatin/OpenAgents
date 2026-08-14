#!/usr/bin/env python3
"""ファイルの置き場所を1箇所に集める。

パスの組み立てをあちこちに散らすと、フォルダを1つ動かしただけで
静かに壊れる（読めないファイルを黙って無視する経路が多いため）。
**ここ以外でリポジトリ内のパスを組み立てないこと。**

ダッシュボード側の同じ役目のファイルは `dashboard/server/paths.ts`。
両者がズレると画面と実体が食い違うので、片方を変えたらもう片方も見ること。
"""

import os

#: このファイル（core/paths.py）の場所
_HERE = os.path.dirname(os.path.abspath(__file__))

#: リポジトリのルート
ROOT = os.path.normpath(os.path.join(_HERE, ".."))

#: 設定の唯一の真実（Botトークン入り・gitignore対象）
CONFIG_PATH = os.path.join(ROOT, "config.json")
#: 同梱の設定例（初期設定の雛形）
CONFIG_EXAMPLE_PATH = os.path.join(ROOT, "config.example.json")

#: 実行時に作られるものは全部ここ（gitignore対象）
STATE_DIR = os.path.join(ROOT, "state")
#: 会話のアーカイブ（全プラットフォーム共通の1本）
DB_PATH = os.path.join(STATE_DIR, "archive.db")
#: リマインダーの永続化
REMINDERS_PATH = os.path.join(STATE_DIR, "reminders.json")
#: 各BOTの生存証明（スーパーバイザが鮮度を見る）
HEARTBEAT_DIR = os.path.join(STATE_DIR, "heartbeat")
#: ログの集約先
LOGS_DIR = os.path.join(STATE_DIR, "logs")

#: 人格ファイル置き場
PERSONAS_DIR = os.path.join(ROOT, "personas")
#: 前提知識ファイル置き場
KNOWLEDGE_DIR = os.path.join(ROOT, "knowledge")
#: 外部連携パッケージ置き場
INTEGRATIONS_DIR = os.path.join(ROOT, "integrations")
#: プラグインスキル（AIが生やす小さな道具）置き場
TOOLS_DIR = os.path.join(_HERE, "tools")

#: 会話セッションを継続するときの固定cwd。
#: claude CLI の --resume はcwdスコープなので、常に同じ場所から起動する必要がある。
SESSION_CWD = ROOT


#: 仮想環境の置き場（セットアップが作る）
VENV_DIR = os.path.join(ROOT, "venv")


def venv_python():
    r"""この環境で使う Python の実行ファイル。

    venv の中身は **Windows と mac/Linux で場所が違う**
    （Scripts\python.exe と bin/python）。ここで吸収しないと、
    片方でしか動かないコードがあちこちに生まれる。
    venv が無ければ、いま動いている Python を返す。
    """
    import sys
    candidates = (
        os.path.join(VENV_DIR, "Scripts", "python.exe"),   # Windows
        os.path.join(VENV_DIR, "bin", "python"),           # mac / Linux
        os.path.join(VENV_DIR, "bin", "python3"),
    )
    for path in candidates:
        if os.path.exists(path):
            return path
    return sys.executable


def ensure_state_dirs():
    """実行時ディレクトリを作る（何度呼んでもよい）。"""
    for d in (STATE_DIR, HEARTBEAT_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)


def resolve(path):
    """設定に書かれた相対パスをリポジトリルート基準の絶対パスにする。

    config.json の `persona_files` などは、利用者が読んで分かるように
    ルート相対（例 "personas/agent1.md"）で書く。絶対パスはそのまま返す。
    """
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(ROOT, path))
