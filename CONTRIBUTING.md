# 開発に参加する

Issue も Pull Request も歓迎します。翻訳（ドキュメントは日本語で書かれています）も
とても助かります。

## 手元で動かす

```bash
git clone <このリポジトリ>
cd OpenAgents
python start.py     # 依存を入れて管理画面を開くところまでやります
```

## テストを回す

```bash
# リポジトリのルートから
python -m unittest discover -s core -t . -q         # 中核（プラットフォーム非依存）
python -m unittest discover -s platforms -t . -q    # Discord実装・開発BOT・議事録BOT

cd dashboard && npm test && npm run typecheck       # 管理画面
```

**この3つが緑でなければマージしません。** CIでも同じものが回ります
（mac / Windows / Linux）。

---

## 🔴 いちばん大事な約束: BOTを変えたらダッシュボードも直す

ダッシュボードは **`config.json` の設定を人間に見せて切り替える画面**です。
BOT側に設定が増えたのに画面に出ていないと、
**「その機能は存在しない」と誤解されて二重に実装されます。** 必ずセットで直してください。

| BOT側でやったこと | ダッシュボード側で直す場所 |
|---|---|
| `agent_loops.py` の `_cycle_plan()` に観察サイクルを追加 | `catalog.proactive.ts` の `CYCLES` |
| `proactive` 直下の共通設定を追加 | `catalog.proactive.ts` の `COMMON` |
| `skills.○○` を追加 | `catalog.agent.ts` の `SKILLS` |
| エージェント直下のフラグを追加 | `catalog.agent.ts` の `BASICS` |
| トップレベル設定を追加 | `catalog.global.ts` の該当グループ |
| 起動時の必須キー・不変条件が増えた | `server/config/schema.ts` の `checkInvariants` |
| 常駐プロセスを追加した | `core/supervisor.py` の `plan_services()` と `server/paths.ts` の `SERVICES` |
| `proactive_log` に新しい `kind` / `action` を書くようにした | `web/lib/format.ts` の `KIND_LABEL` / `ACTION_LABEL` |

### カタログへの足し方

画面は**カタログというデータを描画しているだけ**なので、書くのは1件のオブジェクトです。
フォームは書きません。

```ts
{
  path: "proactive.新機能",
  label: "人間に分かる名前",
  desc: "非エンジニアが読んで分かる1文",
  kind: "tri",                       // 投稿を伴う機能は tri（OFF/シャドー/本番）
  default: { enabled: false, shadow: true },
  children: [ /* 曜日・時刻などの詳細 */ ],
}
```

- `default` には**コード側の実際の既定値**を書いてください
- `desc` はエンジニア向けの説明にしないでください。使う人が読んで判断できる日本語で

---

## 気をつけていること

このプロジェクトが特に大事にしている考え方です。PRを見るときもここを見ます。

### できないことは、できないと言う

- 実行していないのに「やりました」と言わせない
  （`core/honesty.py` が機械的に検出します）
- 対応していない機能は**黙って無効化せず、理由を出す**
  （`core/llm.py` の `describe_limits()`、`core/chat.py` の `CAPABILITIES`）
- 取得できないことと、異常であることを混同しない
  （分からないときは「不明」と言う。「停止」と断定しない）

### 初回起動を壊さない

設定ファイルが無い・DBがまだ無い・BOTが動いていない——これらは
**すべて正常な状態**です。ここでエラーを吐くと、初めて触る人の画面が
真っ赤になって「壊れている」と思われます。

### 層を守る

`core/` からプラットフォーム固有のSDK（`discord` 等）を import しないでください。
`core/test_multiplatform.py` が検査します。

### 設定を壊さない

- 設定の書き込みは `server/config/store.ts` の1箇所だけ
- `config.json` に素の `JSON.parse` / `JSON.stringify` を使わないでください
  → **19桁のIDが丸まって壊れます**。必ず `server/config/bigjson.ts` を使ってください
- 状態を変えるAPIを GET で作らないでください（CSRF防御をすり抜けます）
- 生のconfigをそのまま返すAPIを作らないでください（トークンが漏れます）

---

## コミットメッセージ

```
<type>: <説明>

<本文：なぜそうしたか。何をしたかはdiffを見れば分かる>
```

type は `feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`。

---

## 公開してはいけないもの

CIが検査しますが、念のため。

- `config.json`（Botトークンが入ります）
- `personas/` `knowledge/` の自分用ファイル（見本だけが追跡されます）
- `state/` 以下（DB・ログ・生存証明）
- `.env`

実在の人名・メールアドレス・社内固有の情報をテストの固定値に使わないでください。
