# プラットフォームを追加する

いまは Discord だけが動きます。Slack・LINE・Telegram などを足したい人向けの手順です。

## 全体像

```
core/            ← Discord も Slack も知らない。検索・回答生成・観察ループ
  chat.py        ← ここに書かれた形だけが両者の接点
platforms/
  discord/       ← 実装
  slack/         ← あなたが書く
```

**依存の向きは `platforms/` → `core/` の一方通行**です。逆向き（core が
特定のプラットフォームを import する）は `core/test_multiplatform.py` が
機械的に落とします。「とりあえず core に少しだけ書く」はできません。

## 手順

### 1. 能力を宣言する

`platforms/<名前>/__init__.py` を作り、そのサービスで**実際にできること**を並べます。

```python
from core import chat

NAME = "slack"
LABEL = "Slack"
CAPABILITIES = frozenset({
    chat.CAP_MENTION,
    chat.CAP_THREAD,
    chat.CAP_REACTION,
    chat.CAP_FILE_UPLOAD,
    chat.CAP_HISTORY,
    # CAP_VOICE は無い → 議事録BOTは自動的に無効になる
})
```

**できないことを黙って何もしない実装にしないでください。** 宣言しなければ
中核はその機能を呼びませんし、ダッシュボードにも「なぜ使えないか」が出ます。
宣言だけして中身が空だと、利用者は原因の分からない無反応に悩まされます。

### 2. `ChatPlatform` を実装する

`core/chat.py` の `ChatPlatform` にあるメソッドを埋めます。
必要なのは「読む・書く・さかのぼる」だけです。

| メソッド | 何をするか |
|---|---|
| `start()` / `close()` | 接続の開始と終了 |
| `me()` | 自分（Bot）の情報 |
| `send()` | 投稿して発言IDを返す |
| `react()` | 絵文字を付ける |
| `list_channels()` | チャンネル一覧（設定画面の選択肢になる） |
| `fetch_history()` | 過去ログを古い順に流す |

受け渡しは `ChatMessage` / `ChatChannel` / `ChatUser` / `ChatAttachment` で行います。
**サービス固有のオブジェクトを core に渡さないでください**（`raw` フィールドは
実装内でだけ使う逃げ道です）。

### 3. IDの扱いに気をつける

DBの `id` は整数です（全文検索が整数のrowidを要求するため）。
Discord はスノーフレークがそのまま整数なので `id` に入っていますが、
**Slack の `C01234` のような文字列IDはそのまま入りません。**

```python
from core import db

with db.connect(paths.DB_PATH) as conn:
    # 保存するとき
    db.upsert_channel(conn, id=<採番した整数>, name="general", type="text",
                      platform="slack", external_id="C01234")

    # 引くとき
    internal = db.local_id(conn, "channels", "slack", "C01234")
```

`platform` + `external_id` に一意制約が張ってあるので、同じチャンネルを
二重登録することはできません。プラットフォームが違えば同じ外部IDでも共存できます。

### 4. テストを通す

```bash
python -m unittest discover -s core -t . -q        # 層の分離チェックを含む
python -m unittest discover -s platforms -t . -q
```

`core/` に自分のSDKを import してしまうと `LayeringTest` で落ちます。
これは意地悪ではなく、**その1行が入った瞬間に3つ目のプラットフォームが
実質不可能になる**からです。

## 実装する前に読んでおくとよいもの

- `platforms/discord/__init__.py` — 最小の宣言の例
- `platforms/discord/archiving.py` — 過去ログの取りこぼしゼロ取得の実装
- `platforms/slack/README.md` など — サービスごとの落とし穴を先にまとめてあります

## よくある詰まりどころ

**過去ログが取れないサービスがある**（Telegram / LINE）。この場合は導入直後に
検索できる材料がゼロです。「まだ何も蓄積されていません」と利用者に伝える実装に
してください。空の検索結果を黙って返すと、壊れているように見えます。

**投稿の権限が別に要るサービスがある**。Discord の MESSAGE CONTENT INTENT、
Telegram のプライバシーモードなど、既定では発言を読めない設定になっている
ことが多いです。接続に成功しても発言が届かない場合はここを疑ってください。
`start()` の中で検出できるなら、警告として出してあげると親切です。
