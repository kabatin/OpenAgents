# 設定リファレンス

設定はリポジトリ直下の **`config.json` 1枚だけ**です。
全BOTと管理画面がこれを見ます。

**普段は直接開く必要はありません。** ダッシュボードから変えられます。
この文書は「画面に無い項目を触りたい」「中身を知りたい」ときのためのものです。

## 大事なこと

**設定を変えたら、そのBOTの再起動が要ります。** 設定は起動時に一度だけ
読まれるためです。ダッシュボードの上部に「未適用の変更」バーが出るので、
そこから再起動できます。

**`config.json` は Git で追跡されません。** Botトークンが入るためです。

---

## 全体

```jsonc
{
  "guild_id": "1234567890123456789",   // エージェントが住むDiscordサーバー
  "admins": ["9876543210987654321"],   // 管理者のユーザーID（全体ルールを設定できる人）
  "agent_category_id": null,           // 新チャンネルを作るときのカテゴリ（使わなければ null）

  "history_limit": 10,                 // Bot同士のループ検知に見る件数
  "context_history_limit": 30,         // 文脈として読む会話の件数
  "max_bot_chain": 4,                  // Bot発言が何回続いたら黙るか
  "max_concurrent_answers": 2          // 同時に走らせるAIの数（多いとPCが重い）
}
```

`max_bot_chain` は**エージェント同士が延々と会話し続けるのを止める**ための
上限です。人間が発言するとリセットされます。

---

## 使うAI

```jsonc
"llm": {
  "provider": "claude",         // claude / codex / 自分で定義した名前
  "model": "claude-sonnet-5",   // 空ならツールの既定
  "timeout_sec": 180
}
```

詳しくは [04-llm-providers.md](04-llm-providers.md)。

---

## エージェント

```jsonc
"agents": [
  {
    "id": "agent1",                       // 英小文字。あとで変えない方が無難
    "name": "あかり",                      // Discordでの呼び名
    "token": "...",                       // Botトークン（"${ENV_VAR}" も可）
    "home_channel_id": "1234567890123456780",
    "archiver": true,                     // 会話を記録する担当。ちょうど1体
    "persona_files": ["personas/agent1.md"],
    "role": "",                           // 「何の担当か」の1文
    "require_mention": false,             // true なら呼ばれた時だけ答える
    "runner_enabled": false,              // true でWeb検索などが使える経路になる

    "skills": {
      "reminder": true,                   // リマインダーの登録・配信
      "youtube_summary": true,            // YouTubeのURLを要約
      "pdf_summary": true,                // PDF添付を自動要約
      "image_gen": { "enabled": false }   // 画像生成（連携が要る）
    },

    "proactive": { "enabled": false }     // 観察ループ（下記）
  }
]
```

### `archiver` について

**ちょうど1体が `true`** でなければ起動しません。この1体だけが会話を
DBに記録します（複数だと同じ発言が二重に記録されます）。

### `require_mention`

`false`（既定）だと、ホームチャンネルの人間の発言すべてに答えます。
`true` にすると、名指しされたときだけ答えます。
**チャンネルを静かに保ちたいエージェントはこちら**にしてください。

---

## 観察ループ（自発的な行動）

```jsonc
"proactive": {
  "enabled": true,
  "interval_min": 30,      // 何分おきに見回るか
  "daily_quota": 3,        // 1日に自分から発言してよい回数
  "rest": { "start_hour": 23, "end_hour": 8 },   // 深夜は休む

  "rescue":  { "enabled": true, "shadow": true },
  "briefing": { "enabled": false }
  // …ほかにも多数（ダッシュボードの「全体設定」で一覧できます）
}
```

### 3値トグル（OFF / シャドー / 本番）

発言を伴う機能は3つの状態を持ちます。

| 状態 | 設定 | 何が起きるか |
|---|---|---|
| OFF | `{"enabled": false}` | 何もしない |
| シャドー | `{"enabled": true, "shadow": true}` | **判定して記録するが、発言しない** |
| 本番 | `{"enabled": true, "shadow": false}` | 発言する |

**新しい機能はまずシャドーで動かしてください。** 数日ログを見て、
「これなら発言してよい」と納得してから本番に切り替えるのが安全です。

`daily_quota` は**うるさくならないための上限**です。使い切ったら、
その日はもう自分から話しかけません（聞かれれば答えます）。

---

## 外部連携

```jsonc
"integrations": { "enabled": ["example_notes"] }
```

`integrations/` に置いたフォルダ名を並べます。
さらに、使わせたいエージェントの `skills.<SKILL_KEY>` を `true` にします。
詳しくは [07-integrations.md](07-integrations.md)。

---

## 管理画面

```jsonc
"dashboard": {
  "host": "127.0.0.1",   // 自分のPCだけ。LANに出すなら "0.0.0.0"
  "port": 8787,
  "password": ""         // LANに出すなら必須（ユーザー名は admin）
}
```

**インターネットには公開しないでください。** この画面はBotトークンを
読めるプロセスです。外から使いたい場合は Tailscale などのVPNを使ってください。

---

## 常駐プロセス

```jsonc
"supervisor": {
  "port": 8788,                        // 管理画面との通信用（127.0.0.1固定）
  "restart_backoff_sec": [5, 15, 60]   // 再起動を繰り返すときの待ち時間
}
```

---

## おまけのBOT（既定オフ）

```jsonc
"dev_bot": {
  "enabled": false,
  "token": "",
  "dev_channel_id": ""
},
"meeting_bot": {
  "enabled": false,
  "token": "",
  "voice_channel_id": "",
  "user_mapping": {}
}
```

---

## トークンを設定ファイルに書きたくない

環境変数を参照できます。

```jsonc
"token": "${DISCORD_TOKEN_AGENT1}"
```

リポジトリ直下に `.env` を置くと読まれます（Gitでは追跡されません）。

```
DISCORD_TOKEN_AGENT1=MTIzNDU2...
```

**未定義の変数を参照するとエラーになります。** 空文字に潰して
「起動しないBOT」を作らないためです。

---

## 知らないキーは消えません

`_comment` のような、この仕組みが知らないキーを書いても大丈夫です。
ダッシュボードから保存しても、そのまま残ります
（設定ファイルは人が育てるものなので、勝手に消しません）。
