# コマンドで操作する

`openagents` コマンドは、**導入と更新の入口**です。
中身の面倒（venv・依存・画面の組み立て）は従来どおり `start.py` が見ます。

```bash
npm install -g openagents
openagents setup
```

git clone で入れている人は、このコマンドを使わなくても何も困りません。
入れておくと `openagents update` が使えるようになります。

---

## コマンド一覧

| コマンド | 何をするか |
|---|---|
| `openagents setup` | 置き場に取得して、設定画面をブラウザで開く。**無引数のときはこれ** |
| `openagents start` | 常駐プロセスを前面で動かす（`python run.py` と同じ。Ctrl-C で止まる） |
| `openagents stop` | 動いている常駐プロセスを止める |
| `openagents status` | 置き場・バージョン・BOTごとの生死 |
| `openagents update` | 最新に更新する。`--restart` を付けると常駐も入れ替える |
| `openagents where` | 置き場のパスと、**なぜそこになったか** |

| オプション | |
|---|---|
| `--dir <path>` | 置き場を指定する |
| `--restart` | `update` のあと、常駐プロセスを入れ替える |
| `--version` | CLI と本体、両方のバージョン |

---

## 置き場はどこになるか

このプロジェクトは `config.json` も会話の記録も性格ファイルも、
**すべてリポジトリ直下**に置きます（`core/paths.py` の `ROOT`）。
つまり「置き場」＝ リポジトリを取得した場所そのものです。

次の順で決まります。

1. `--dir <path>`
2. 環境変数 `OPENAGENTS_HOME`
3. **いま居る場所が OpenAgents の中なら、そこ**
4. `~/.openagents`（既定）

3番目があるので、**git clone で入れた人が npm の CLI を後から入れても、
自分の checkout がそのまま操作対象になります**。`~/.openagents` に
もう1つ落ちてきて、どちらを見ているか分からなくなる、ということは起きません。

いま何が使われているか迷ったら:

```bash
$ openagents where
/Users/you/work/OpenAgents
  （いま居る場所 で決まりました）
```

複数の環境（本番用と試用など）を使い分けるなら、環境変数が楽です。

```bash
OPENAGENTS_HOME=~/openagents-test openagents setup
```

---

## 更新する

```bash
openagents update              # 取得するだけ
openagents update --restart    # 取得して、動いている常駐も入れ替える
```

やっていることは `git pull --ff-only` です。npm で入れた人も git clone で
入れた人も**同じ1本の経路**を通ります。取得したあとは `start.py` の
準備処理をそのまま呼ぶので、`requirements.txt` が増えていれば入れ直され、
管理画面のコードが変わっていれば組み立て直されます。

**手元に変更があるときは、更新せずに止まります。** 勝手に退避も破棄もしません。

```
❌ 手元に変更があるため、更新を見送りました

   M personas/agent1_tone.md

  変更を残すなら退避を、要らないなら破棄をしてから、もう一度実行してください:
```

性格ファイル（`personas/`）や前提知識（`knowledge/`）は gitignore されているので、
ここに出てくることは通常ありません。出てきたら、追跡されているファイルを
自分で書き換えている、ということです。

### `--restart` は何を入れ替えるのか

**常駐プロセス（`run.py`）ごと**入れ替えます。ダッシュボードの「再起動」は
個々のBOTを起こし直すだけなので、更新後は `run.py` 自身が古いコードを
抱えたまま残ります。それでは更新が半分しか届きません。

`openagents stop` と `--restart` は、`run.py` の操作用API（`127.0.0.1:8788`）に
`POST /shutdown` を送ります。この窓口は **v0.1.2 以降**にしかないので、
それより古い版から上げるときは、1回だけ手で止め直してください。

---

## npm を使わない場合

CLI が無くても、やることは変わりません。

```bash
cd <置き場>
git pull --ff-only
python start.py     # 依存の入れ直しと画面の組み立てまでやってくれます
```

---

## 困ったとき

- `openagents where` が意図しない場所を指す → `--dir` か `OPENAGENTS_HOME` で明示する
- `Python が見つかりません` → [04 使うAIを選ぶ](04-llm-providers.md) の前に、
  まず Python 3.10以上を入れてください
- それ以外 → [09 困ったとき](09-troubleshooting.md)
