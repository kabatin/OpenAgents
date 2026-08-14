# Slack 対応（未実装）

**まだ動きません。** ここにあるのは「どう作れば繋がるか」の手順だけです。
実装のプルリクエストを歓迎します。

## やること

1. `platforms/slack/__init__.py` に `NAME` / `CAPABILITIES` / `LABEL` を宣言する
   （`platforms/discord/__init__.py` が最小の見本です）
2. `core.chat.ChatPlatform` の各メソッドを実装する
3. `core/test_multiplatform.py` の層チェックを通す（`core/` を汚さない）

詳しい手順は `docs/10-adding-platforms.md` にあります。

## Slack 特有の注意

### IDが文字列

Slack のIDは `C01234ABCDE`（チャンネル）や `U01234ABCDE`（ユーザー）のような
文字列です。DBの `id` は整数のままなので、**必ず `core.db.local_id()` を通して**
`platform="slack"` + `external_id="C01234ABCDE"` から内部IDを引いてください。
Discord のようにIDをそのまま `id` に入れることはできません。

### 対応できる能力

| 能力 | Slack | 備考 |
|---|---|---|
| `mention` | ○ | `<@U01234>` 形式 |
| `thread` | ○ | `thread_ts` を使う。Discordのスレッドとは意味が少し違う |
| `reaction` | ○ | `reactions.add` |
| `persona_post` | ○ | `chat.postMessage` の `username` / `icon_url` |
| `file_upload` | ○ | `files.upload_v2` |
| `history` | △ | 無料プランは直近90日しか遡れない |
| `voice` | × | 通話音声のAPIは公開されていない（議事録BOTは動きません） |

### 接続方式

Socket Mode（WebSocket）を推奨します。Events API の HTTP エンドポイント方式は
外部から到達できるURLが必要になり、「自宅のPCで動かす」という前提に合いません。

### メッセージIDが `ts`

Slack の発言IDは `"1728000000.123456"` というタイムスタンプ文字列です。
チャンネル内でしか一意でないため、`external_id` には
`"<channel>:<ts>"` のように連結した値を入れてください。
