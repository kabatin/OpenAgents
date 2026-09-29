# スクリーンショットの撮り直し

README とドキュメントの画面画像（`docs/images/*.png`）を、架空のデモデータで撮り直します。

```bash
./venv/bin/python docs/images/capture/run.py
```

- リポジトリを一時ディレクトリへコピーし、その中で設定・デモデータ・偽のスーパーバイザ
  （全BOT稼働中を返す）を用意して撮ります。**本物の `state/` と `config.json` には触れません**
- 管理画面は空いているポートで立て、終わったら自分が立てたプロセスだけを止めます
- 各画像は中身の外接矩形＋余白で切り出します（空白の多い画像にしない）

必要なもの: `dashboard/node_modules`（`npm ci` 済み）、Google Chrome、リポジトリの venv（Pillow）。
worktree など node_modules が無い場所で動かすときは `OA_NODE_MODULES=<場所>` を付けます。
Chrome の場所が違うときは `CHROME=<実行ファイル>` を付けます。

| ファイル | 役割 |
|---|---|
| `run.py` | 全体の段取り（コピー・設定・起動・撮影・後片付け） |
| `seed.py` | デモデータ（会話・自発行動の記録・追跡タスク・LLM呼び出しなど。すべて架空） |
| `capture.mjs` | ヘッドレス Chrome を DevTools Protocol で操作して撮る（依存なし） |

画面を変えたら、撮る画面と操作は `capture.mjs` の末尾の `shoot(...)` で調整します。
