# 開発ガイドライン（開発BOTの実装規約）

このファイルは改修プロンプトへそのまま注入される「データとしてのルール」。
規約の改善はコード変更ではなくこのファイルの改修（起票→👍承認）で行える。
（欠損時は dev_pipeline.DEFAULT_GUIDELINES の最小規約で動き続ける）

## 最重要・統合方針
- **まず既存の関連実装を Grep/Glob/Bash で探して読むこと**。いきなり新規ファイルを作らない。
- 機能の多くは scripts/discord-archive/ にある（bot.py, reminders.py, rules.py, db.py,
  agent_runtime.py 等）。既存機能の拡張なら**その既存モジュールを直接改修して統合する**。
  孤立した新規ファイルを作るのは、本当にそれが正しい設計の時だけにする。

## discord-archive のモジュール地図（新機能をどこに書くか）
- **bot.py は太らせない**（トリガー判定・応答フロー・起動処理のみ）。新機能の配線は種別で:
  - 観察ループ系（定期的に見て動く）→ ロジックは新モジュール、配線は
    **agent_loops.py の `_cycle_plan()` に1行**＋サイクルメソッド追加
  - プリフック型スキル（投稿を見て即発動）→ **skill_hooks.py**
  - **エージェントが使う能力（読む・書く）→ archive_tools の tools_read.py / tools_write.py に
    `Tool` を register する（v4 以降の標準）**。LLM の回答途中でツールとして呼ばれ、
    結果（ok / evidence）を見てから本文が書かれる。description には「いつ使うか・書き忘れると
    何が起きるか」を書き、evidence の -# 行は honesty.py の SUCCESS/FAIL_DEEDS と同じ書式にする。
    権限（管理者・本人・上限）はツール内のコードで判定し、LLM の申告を信用しない
  - マーカー型（LLM出力を正規表現で拾って副作用）→ **旧方式**。新規には使わない
    （marker_actions.py は残っているが、ツールループ本番では除去のみ）
  - リアクション起点 → **reaction_handlers.py**
- 外向き機能は config フラグ（既定オフ）＋シャドーモード（下記）。判定・整形の
  純粋ロジックは独立モジュールに置き、mixin からは薄く呼ぶ。
- 既存のコード規約・命名・粒度に合わせ、単一責任/KISS を保ち、差分は最小限に。
- **中核（bot.py / agent_runtime.py / agent_loops.py / honesty.py / rules.py / db.py /
  invoke_claude.py / archive_tools の registry・server・launch）は原則触らない**。新しい能力は
  ツールと独立モジュールで足す。中核に触れざるを得ない場合は要約で理由を明記する
  （承認者に 🧠 の警告が出る）。
- 純粋関数（テスト対象）とIOを分離する。このリポジトリの既存モジュールがその手本。

## 使ってよい手段
- Bash を使ってよい（コード探索の grep、テスト実行など）。ただし秘密ファイル
  （config.json / .env / *.db / auth*）は読まない・出力しない・コピーしない。
- 外部ネットワークへのアクセス（curl/wget等）とパッケージ追加（pip install）は禁止。
  依存追加が必要だと判断したら、実装せず最終要約で「必要な依存」として申告する。
- 振る舞いを変える場合は必ず対応するテストを追加/更新し、可能なら自分で
  unittest を回して緑を確認すること（`venv/bin/python -m unittest discover -s core -t . -q`。
  pytest ではなく unittest。システムの python3 では discord が無く動かない）。
  最終的にパイプラインも検証する。
- 回答の内容に影響する変更（指示文・ツールの description・注入）は、要約に
  「golden_eval で回帰確認が必要」と明記する（パイプラインは回さない）。

## 設定を増やしたら管理ダッシュボードのカタログも直す（必須）

config フラグを新設・変更したら、**同じ改修の中で**
`scripts/dashboard/server/config/catalog.*.ts` にも1件足すこと。
片方だけだと「動いているのに画面に出ない機能」または「画面にあるのに効かないトグル」が
生まれ、人間が実態を誤解する。

- `_cycle_plan()` にサイクル追加 → `catalog.proactive.ts` の `CYCLES`
- `skills.○○` を追加 → `catalog.agent.ts` の `SKILLS`
- エージェント直下のフラグ → `catalog.agent.ts` の `BASICS`
- トップレベル / `dev_bot.*` → `catalog.global.ts`

足すのはオブジェクト1個（フォームは書かない）。`label` と `desc` は
**使う人が読んで判断できる日本語**にする。`default` はコード側の実際の既定値。
投稿を伴う機能は `kind: "tri"`（OFF/シャドー/本番）にして、既定はシャドー。

検証: `cd scripts/dashboard && npm test && npm run typecheck`
（依存の追加は禁止。カタログ追記に新しい依存は要らない）

## 新機能の安全規約（フラグとシャドー）
- **外向きの新機能**（Discordへ新しい投稿・メンション・リアクションを増やすもの）は
  必ず config のフラグ（既定オフ）または既存の proactive 設定配下に載せること。
  フラグ無しで常時発動する実装にしない（異常時に「設定1行で止められる」を保証する）。
- **自発的な投稿を伴う機能はシャドーモードを既定にする**: 実際には投稿せず、
  proactive_log 等に「こう投稿するつもりだった」を記録するだけの状態で本番投入し、
  実データでの判断の質を人間が確認してからフラグで本投稿を解禁する
  （worktreeでは実挙動を検証できないことへの対策。v3のproactive_logが手本）。

## 厳守する規約
- 変更してよいのは scripts/ 配下のみ。次のファイルへの書き込みは拒否される:
  dev_gate.py / deploy.py / gate.py / settings.json / config.json / .env / *.plist / *.db。
- 完了したら、最後に「変更したファイル」と「何をどう変えたか」を3〜6行で要約する。
