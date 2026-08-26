# 何を変えたか

<!-- 何をしたかは diff で分かるので、**なぜそうしたか**を書いてください -->

関連 Issue: #

## 動かして確かめたこと

<!-- 実際に手元でどう確認したか。「テストが通った」だけでなく、
     Discord で / ダッシュボードで どう見えたかも書けると助かります -->

## チェック

- [ ] `python -m unittest discover -s core -t . -q` が緑
- [ ] `python -m unittest discover -s platforms -t . -q` が緑
- [ ] `cd dashboard && npm test && npm run typecheck` が緑
- [ ] **設定を新設した場合**、同じコミットで `dashboard/server/config/catalog.*.ts` にも足した
      （足し忘れると「動いているのに画面に無い機能」になります → [CONTRIBUTING.md](../CONTRIBUTING.md#-いちばん大事な約束-botを変えたらダッシュボードも直す)）
- [ ] `core/` からプラットフォーム固有のSDK（`discord` など）を import していない
- [ ] `config.json` を読み書きする場合、素の `JSON.parse` / `JSON.stringify` ではなく
      `server/config/bigjson.ts` を使った（19桁IDが丸まるため）
- [ ] トークン・実在の人名・社内固有の情報を混入させていない

## 影響範囲

<!-- 既存の設定・DB・挙動が変わる場合はここに。既定値を変えたなら必ず書いてください -->

- [ ] 破壊的変更はない
- [ ] 破壊的変更がある（内容: ）
