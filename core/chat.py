#!/usr/bin/env python3
"""会話プラットフォームの抽象。

`core/` は Discord も Slack も知らない。会話の読み書きは全部ここで定めた形を
通して行い、実際のやりとりは `platforms/<名前>/` の実装が引き受ける。

**この形はDBスキーマから決めた。** `core/db.py` の channels / users / messages /
attachments が、既に「どのサービスでも共通に持てる最小限」になっていたので、
新しい理想形を発明せず、実際に保存している形をそのまま契約にしている。

## 実装する側がやること

1. `ChatPlatform` の各メソッドを実装する
2. `CAPABILITIES` に、そのサービスで**実際にできること**だけを並べる
3. `platforms/<名前>/__init__.py` で公開する

できないことを黙って no-op にしないこと。`CAPABILITIES` に書かなければ、
中核側がその機能を呼ばないし、ダッシュボードでも灰色表示になる。

詳しい手順は docs/10-adding-platforms.md。
"""

from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, Protocol, Sequence, runtime_checkable

# --- 能力の名前 ---------------------------------------------------------
# サービスによって「できること」が違う。中核はここを見て機能を出し分ける。

#: 特定の相手を名指しして呼べる（@メンション）
CAP_MENTION = "mention"
#: 発言にぶら下がるスレッドを開ける
CAP_THREAD = "thread"
#: 発言に絵文字リアクションを付けられる
CAP_REACTION = "reaction"
#: 1つの接続から、名前とアイコンを変えて複数人格として投稿できる（Webhook等）
CAP_PERSONA_POST = "persona_post"
#: ファイルを添付して投稿できる
CAP_FILE_UPLOAD = "file_upload"
#: 過去ログをさかのぼって取得できる
CAP_HISTORY = "history"
#: ボイスチャンネルの音声を受け取れる（議事録BOTが要求する）
CAP_VOICE = "voice"

#: 全能力の一覧（ダッシュボードの表示と検証に使う）
ALL_CAPABILITIES = (
    CAP_MENTION, CAP_THREAD, CAP_REACTION, CAP_PERSONA_POST,
    CAP_FILE_UPLOAD, CAP_HISTORY, CAP_VOICE,
)

#: 人間に見せる説明（ダッシュボードで「なぜ使えないか」を出すため）
CAPABILITY_LABELS = {
    CAP_MENTION: "メンション（名指しで呼ぶ）",
    CAP_THREAD: "スレッド返信",
    CAP_REACTION: "絵文字リアクション",
    CAP_PERSONA_POST: "1接続で複数人格として投稿",
    CAP_FILE_UPLOAD: "ファイル添付",
    CAP_HISTORY: "過去ログの取得",
    CAP_VOICE: "音声の受信（議事録）",
}


# --- 正規化したデータ型 -------------------------------------------------
# ID は必ず str。Discord は19桁の数値、Slack は "C01234" のような文字列で、
# 型を揃えないと保存も比較もプラットフォームごとに分岐してしまう。


@dataclass(frozen=True)
class ChatUser:
    """発言者。Bot も人間もこれで表す。"""

    id: str
    #: ログイン名・アカウント名（一意）
    name: str
    #: 表示名（変わりうる。無ければ name と同じ）
    display_name: str = ""
    is_bot: bool = False

    def __post_init__(self):
        if not self.display_name:
            object.__setattr__(self, "display_name", self.name)


@dataclass(frozen=True)
class ChatChannel:
    """チャンネル・ルーム・トークの類。"""

    id: str
    name: str
    #: "text" | "voice" | "thread" | "dm" など。保存はするが中核は分岐しない
    type: str = "text"
    #: スレッドの親、カテゴリなど。無ければ None
    parent_id: Optional[str] = None


@dataclass(frozen=True)
class ChatAttachment:
    """添付ファイル。**実体は保存しない**（メタとURLだけ持つ）。"""

    id: str
    filename: str
    content_type: str = ""
    size: int = 0
    url: str = ""

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def is_video(self) -> bool:
        return self.content_type.startswith("video/")


@dataclass(frozen=True)
class ChatMessage:
    """1発言。core/db.py の messages テーブルと同じ形。"""

    id: str
    channel_id: str
    author: ChatUser
    content: str
    #: ISO8601 の文字列（タイムゾーン付き）
    created_at: str
    edited_at: Optional[str] = None
    #: 返信元のメッセージID
    reply_to: Optional[str] = None
    #: スレッド内の発言ならそのスレッドID
    thread_id: Optional[str] = None
    attachments: Sequence[ChatAttachment] = field(default_factory=tuple)
    #: この発言で名指しされた相手
    mentions: Sequence[str] = field(default_factory=tuple)
    #: 元のプラットフォーム側オブジェクト（実装内でだけ使う。core は触らない）
    raw: object = None


@dataclass(frozen=True)
class ChatReaction:
    """発言に付いた絵文字リアクション。"""

    message_id: str
    channel_id: str
    user_id: str
    emoji: str
    #: True=付いた / False=外れた
    added: bool = True


# --- インターフェース ---------------------------------------------------


@runtime_checkable
class ChatPlatform(Protocol):
    """会話プラットフォーム1つ分の実装。

    中核が必要とするのは「読む・書く・さかのぼる」だけ。
    それ以外（サービス固有の機能）は実装側に閉じ込める。
    """

    #: "discord" / "slack" / "line" / "telegram"
    name: str
    #: このサービスで実際にできることの集合
    capabilities: frozenset

    async def start(self) -> None:
        """接続してイベントの受信を始める（接続が切れるまで返らない）。"""
        ...

    async def close(self) -> None:
        """接続を閉じる。"""
        ...

    async def me(self) -> ChatUser:
        """自分（このBot）の情報。"""
        ...

    async def send(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: Optional[str] = None,
        files: Optional[Sequence[str]] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """投稿して、作られた発言のIDを返す。

        thread_id / files は、対応する能力を宣言していない実装では
        無視してよい（呼ぶ側が capabilities を見て使い分ける）。
        """
        ...

    async def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        """発言に絵文字を付ける（CAP_REACTION）。"""
        ...

    async def list_channels(self) -> Sequence[ChatChannel]:
        """見えているチャンネルの一覧。設定画面の選択肢にも使う。"""
        ...

    def fetch_history(
        self, channel_id: str, after_id: Optional[str] = None, limit: int = 200
    ) -> AsyncIterator[ChatMessage]:
        """過去ログを古い順に流す（CAP_HISTORY）。

        after_id より後だけを取る＝取りこぼしゼロの追記を成立させる。
        """
        ...


# --- 発言へのリンク -------------------------------------------------------
#
# 「この発言が根拠です」と示すためのリンクは、URLの形がサービスごとに違う。
# core がその形を知っていると、そこだけ Discord 専用になってしまうので、
# **実装側が登録し、core は登録されたものを使う**という形にする。

#: platform名 -> (リンクを作る関数, リンクを読む正規表現)
_LINK_FORMATS = {}


def _discord_link(guild_id, channel_id, message_id):
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


#: Discord のリンク形式を既定として持っておく。
#:
#: **なぜ core にあるのか**: Discord はこのリポジトリが同梱する唯一の実装で、
#: `platforms/discord` を import しない経路（テストや単体利用）でも
#: リンクが要るため。これは「依存」ではなく「データ」で、core は
#: discord.py も Discord のオブジェクトも触らない。
#:
#: 別のプラットフォームは register_links() で**自分の形式を登録**する。
#: 引き方は platform 名で分かれるので、Slack のメッセージに Discord の
#: リンクが付く、ということは起きない。
_LINK_FORMATS["discord"] = (
    _discord_link,
    __import__("re").compile(
        r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"),
)


def register_links(name, build, pattern):
    """プラットフォームが自分のリンク形式を登録する。

    build(guild_id, channel_id, message_id) -> URL
    pattern: URLから (guild, channel, message) を取り出す compiled regex
    """
    _LINK_FORMATS[name] = (build, pattern)


def link_to(platform, guild_id, channel_id, message_id):
    """発言へのリンクを作る。未登録のプラットフォームなら空文字。

    空文字を返すのは意図的。**知らない形のURLをでっち上げない**
    （リンク先が存在しない「もっともらしいURL」は、無いより悪い）。
    """
    entry = _LINK_FORMATS.get(platform)
    if entry is None:
        return ""
    return entry[0](guild_id, channel_id, message_id)


def link_pattern(platform):
    """そのプラットフォームのリンクを読む正規表現（未登録なら None）。"""
    entry = _LINK_FORMATS.get(platform)
    return entry[1] if entry else None


def registered_platforms():
    return sorted(_LINK_FORMATS)


def missing_capabilities(platform, required) -> list:
    """要求する能力のうち、このプラットフォームに無いものを返す。

    機能を黙って無効化せず、「何が無いから使えないのか」を人間に見せるために使う。
    """
    have = getattr(platform, "capabilities", frozenset())
    return [c for c in required if c not in have]


def describe_missing(missing) -> str:
    """不足能力を人間に読める1文にする。"""
    if not missing:
        return ""
    names = "・".join(CAPABILITY_LABELS.get(c) or c for c in missing)
    return f"このプラットフォームは {names} に対応していません"
