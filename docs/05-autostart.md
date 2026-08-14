# PCを起動したら自動で立ち上げる

エージェントに常駐してもらうには、PCのログイン時に `run.py` が動き出すように
登録します。**OSに登録するのはこの1本だけ**です。個々のBOTの起動・再起動・
ログの面倒は `run.py`（スーパーバイザ）が見るので、BOTを増やしても
この設定は変わりません。

## その前に

まず手で動かして、ちゃんと動く状態か確かめてください。

```bash
python run.py
```

こう表示されれば準備できています。

```
============================================================
OpenAgents を起動しました
============================================================
  [起動] 会話エージェント  — 1体が1プロセスで動くため、再起動は全員同時になります
  [オフ] 開発BOT  — Discordから開発を指示できます（既定はオフ）
  [オフ] 議事録BOT  — 会議が無い間は無音が正常です（既定はオフ）
```

「設定が足りないため、まだBOTを起動できません」と出た場合は、
先に `python start.py` でダッシュボードを開いて設定してください。

## macOS

```bash
./autostart/install-macos.sh
```

解除するときは `--remove` を付けます。

```bash
./autostart/install-macos.sh --remove
```

**PATH について**: launchd から起動されるとPATHが最小限になり、`claude` や
`codex` を見つけられないことがあります。登録スクリプトは
`~/.local/bin` `/opt/homebrew/bin` `/usr/local/bin` を足していますが、
それ以外の場所に入れている場合は plist の `PATH` に追記してください。

## Windows

PowerShell で実行します（管理者権限は不要です）。

```powershell
powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1
```

解除するときは `-Remove` を付けます。

```powershell
powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1 -Remove
```

**コンソール窓について**: `pythonw.exe` があればそちらを使うので、
黒い窓は出ません。出てしまう場合は `venv\Scripts\pythonw.exe` が
存在するか確認してください。

## ダッシュボードからも登録できます

「運用」画面の自動起動ボタンからも同じことができます。
コマンドを打つのが不安な場合はそちらをどうぞ。

## 動いているか確かめる

いちばん確実なのはダッシュボードの「運用」画面です。各BOTの状態・稼働時間・
再起動回数が出ます。コマンドで見るなら:

```bash
curl -s http://127.0.0.1:8788/status
```

`run.py` が動いていなければ、ここに繋がりません
（ダッシュボードにも「常駐プロセスに接続できません」と出ます）。

## 議事録BOTを使う場合の追加作業

議事録BOTは音声を扱うため、追加のインストールが必要です。
**既定ではオフ**なので、使うときだけ入れてください。

```bash
# mac / Linux
./venv/bin/pip install -r platforms/discord/meeting/requirements.txt

# Windows
venv\Scripts\pip install -r platforms\discord\meeting\requirements.txt
```

文字起こしモデル（faster-whisper）は初回実行時にダウンロードされます。
数百MBあるので、最初の会議だけ時間がかかります。

## うまく動かないとき

**ログインしても起動しない** — `state/logs/supervisor.log` を見てください。
Python のパスが変わった（venv を作り直した等）場合は、登録スクリプトを
もう一度実行すれば直ります。

**すぐ落ちて再起動を繰り返す** — `state/logs/<BOT名>.log` に理由が出ています。
よくあるのはトークンの間違い（`Improper token has been passed.`）です。
ダッシュボードの「運用」画面にも再起動回数が出るので、
数字が増え続けていればクラッシュループです。

**スリープから復帰すると止まっている** — mac は `KeepAlive` で、
Windows は再試行設定で起こし直すようにしてありますが、
ネットワークの復帰が遅いと数分かかることがあります。
それでも戻らない場合はダッシュボードから再起動してください。
