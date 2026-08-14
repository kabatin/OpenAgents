# Telegram 対応（未実装）

**まだ動きません。** 実装のプルリクエストを歓迎します。
手順は `docs/10-adding-platforms.md`、最小の見本は `platforms/discord/__init__.py`。

## Telegram 特有の注意

### 「チャンネル」の考え方が違う

Telegram にはグループ・スーパーグループ・チャンネル・個人チャットがあり、
Discord の「1サーバーに複数チャンネル」という構造がありません。
**1グループ = 1チャンネル**として扱い、`config.json` の `guild_id` には
グループのIDを入れるのが素直です。

### 対応できる能力

| 能力 | Telegram | 備考 |
|---|---|---|
| `mention` | ○ | `@username`。ユーザー名未設定の相手は名指しできない |
| `thread` | △ | フォーラム形式のグループのみ（`message_thread_id`） |
| `reaction` | ○ | Bot API 7.0 以降 |
| `persona_post` | × | 1Bot=1人格。複数人格には複数のBotを登録する |
| `file_upload` | ○ | `sendDocument` |
| `history` | × | **Bot は過去ログを取得できない**（重要） |
| `voice` | × | 通話音声は取得できない |

### 過去ログが取れないことの影響

Telegram の Bot API には「参加前のメッセージを遡って取得する」手段がありません。
つまり `archiver` が起動した**後**の会話しか蓄積できません。
検索して答える機能は動きますが、導入直後は答えられる材料がゼロです。
その旨を利用者に伝える実装にしてください（黙って空の検索結果を返さない）。

### プライバシーモード

グループ内のBotは既定でコマンド以外のメッセージを受け取れません。
BotFather で `/setprivacy` を Disabled にする必要があります
（Discord の MESSAGE CONTENT INTENT に相当する、最大の脱落ポイントです）。
