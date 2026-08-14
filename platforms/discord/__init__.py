"""Discord 実装。

`core.chat.ChatPlatform` に対応する実体は `agent_runtime` / `bot` /
`archiving` が分担している（1プロセスで複数Botアカウントを動かす都合で、
1クラスに畳んでいない）。ここでは**このプラットフォームで何ができるか**を
宣言する。中核とダッシュボードはこれを見て機能を出し分ける。
"""

import re

from core import chat

#: このプラットフォームの識別子（DBの platform 列に入る値）
NAME = "discord"

#: Discord で実際にできること。
#: 音声はライブラリを追加インストールした場合のみなので、ここには入れない
#: （議事録BOTが自分で判定する）。
CAPABILITIES = frozenset({
    chat.CAP_MENTION,
    chat.CAP_THREAD,
    chat.CAP_REACTION,
    chat.CAP_PERSONA_POST,   # Webhook で名前とアイコンを差し替えられる
    chat.CAP_FILE_UPLOAD,
    chat.CAP_HISTORY,
})

#: 人間向けの表示名
LABEL = "Discord"

# --- 発言へのリンク -------------------------------------------------------
# 形式: https://discord.com/channels/<guild>/<channel>/<message>
# discordapp.com / canary.discord.com などの派生ドメインも読めるようにする
LINK_RE = re.compile(
    r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def build_link(guild_id, channel_id, message_id):
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


chat.register_links(NAME, build_link, LINK_RE)
