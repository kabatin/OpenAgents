# 全体設計

チャットの全会話を SQLite に蓄積する取込基盤と、その上で動くAIエージェント。
**1プロセスで複数のBotアカウントを同時に動かします**（3体いても3プロセスにはなりません）。

## 層の分け方

```
core/           プラットフォーム非依存。Discord も Slack も知らない
  chat.py       会話プラットフォームの抽象（実装が満たす契約）
  db.py         会話アーカイブ（SQLite + trigram全文検索）
  search.py     検索して答えを作る
  llm.py        どのAIに考えさせるか
  supervisor.py BOTたちの親プロセス
platforms/
  discord/      Discord実装。ここだけが discord.py を import する
```

依存の向きは **platforms → core の一方通行**です。`core/` が特定の
プラットフォームを import していないことを `core/test_multiplatform.py` が
機械的に検査します。人間のレビューでは見落とすうえ、その1行が入った瞬間に
3つ目のプラットフォームが実質不可能になるためです。

## エージェント構成

エージェントは `config.json` の `agents` 配列で定義します。何体でも増やせます。
`archiver: true` の1体だけが会話をDBに記録し、**記録は全員で共有**します。

### 応答ルール
- **ホームch**: 人間の発言すべてに回答（他エージェントが名指しされていれば沈黙）。
  ただし `require_mention: true` のエージェントはホームchでも**メンション時のみ**応答
  （チャンネルを静かに保つ。2体目以降の既定はこちら）
- **メンション**: 人間からの自分宛メンションには guild 内のどこでも回答
- **エージェント連携**: 登録エージェント同士は相互メンションで会話できる。
  直近履歴の連続Bot発言数+1 が `max_bot_chain`(4) 以上になると自動停止
  （人間の発言でリセット。ログに `chain limit reached` が出る）
- 未登録Bot・webhook（議事録botなど）には一切反応しない
- 全エージェントが共通の `archive.db` を検索して回答（＝共有ナレッジ。
  他チャンネルの議論も踏まえられる）

## ⚠️ 設定を増やしたらダッシュボードも直す

設定はダッシュボードの画面から切り替えられます。**設定を新設したら、
同じコミットで `dashboard/server/config/catalog.*.ts` に1件足してください。**
片方だけだと「動いているのに画面に無い機能」になり、無いものとして再実装されます。

詳しくは [CONTRIBUTING.md](../CONTRIBUTING.md) の対応表を見てください。

## 設計方針

- **添付の実体は保存しない。** `message_id` と最小メタだけ記録し、参照は
  Discordのジャンプリンクで復元する。
- **取りこぼしゼロ。** 起動時に各チャンネルの「保存済み最大ID以降」だけを取得。
  アーカイブ書込は archiver の1体のみ（重複書込なし）。
- 回答生成は `claude` CLI（APIキー・追加課金なし）。
  キーワード抽出 → trigram全文検索 → ペルソナ注入で回答。

## config.json スキーマ

```json
{
  "guild_id": "...",
  "history_limit": 10,
  "context_history_limit": 30,
  "max_bot_chain": 4,
  "max_concurrent_answers": 2,
  "agents": [
    {"id": "agent1", "name": "エージェント1", "token": "...",
     "home_channel_id": "...", "archiver": true,
     "persona_files": ["../../IDENTITY.md", "personas/agent1_tone.md"], "role": ""},
    {"id": "agent2", "name": "エージェント2", "token": "...",
     "home_channel_id": "...",
     "persona_files": ["personas/design.md"],
     "role": "デザイン担当。…",
     "skills": {"image_gen": {"backend": "imagegen", "timeout_sec": 300}}}
  ]
}
```

- `history_limit`(既定10) はループガードの窓。`context_history_limit`(既定30) が
  「直近の会話」として応答に渡す過去メッセージ件数（会話の連続性用・別枠）
- `token` が空のエージェントは起動時にスキップされる（ログに出る）
- `archiver: true` はちょうど1体。persona_files はこのディレクトリ基準の相対パス
- **config.json と archive.db は gitignore 済み（トークン入りのためコミット禁止）**

## エージェントの増員手順

1. Discord Developer Portal → New Application → Bot タブで
   **MESSAGE CONTENT INTENT / SERVER MEMBERS INTENT を ON**（忘れると本文が空で無反応）、
   Public Bot OFF、Reset Token でトークン取得
2. OAuth2 URL Generator: scope=`bot`、権限 = View Channels / Send Messages /
   Send Messages in Threads / Read Message History / Embed Links → サーバーへ招待
3. `personas/<id>.md` を作成（IDENTITY.md の章構成＋末尾に【話し方】）
4. `config.json` の agents に追記 → 再起動

## 運用

```bash
# 常駐（launchd）
launchctl kickstart -k gui/$(id -u)/com.discord.archivebot   # 再起動
tail -f bot.log                                              # ログ

# 単体テスト（Discord抜きで回答を試す）
./venv/bin/python ask.py --agent design "バナーどうしたらいい？"

# ユニットテスト
./venv/bin/python -m unittest test_units -v

# 模範Q&Aの回帰採点（週1回、cron や launchd で回す想定・Claude Code の費用が掛かる）
./venv/bin/python -m core.golden_eval --set curated
# 結果は state/golden_eval/<日時>.json に残り、最新の平均点が週次レポートに出る
```

### 観察ループの遅延隔離

観察ループは多数のサイクルを直列に回す。1サイクルが LLM の待ちで固まると
後続が丸ごと遅れるため、各サイクルを `asyncio.wait_for`（900秒）で包み、
超過は打ち切って `proactive_log`（action=cycle_timeout）に残す。60秒を超えた
サイクルは所要時間をログに出す（どれが遅いかを見てから統合を判断する）。

## 追跡タスクの統一入口（core/tasks.py）

議事録TODO（action_items）・宿題（homework_items）・リマインダー（reminders.json）は
テーブルを統合せず、`core/tasks.py` が **統一ビュー** に読み替える。
key は種別の頭文字＋id（A6 / H27 / R57）。`list_all` が3種を期日順で返し、
`update(key, action)` が種別に振り分ける（done / cancel / due / open。本人か管理者）。
出口として、超過の声かけから `STALE_DAYS`（7日）動きが無い TODO は `stale` にして
1行で手放す（`action_items.overdue_at` が起点）。
ダッシュボードの「データ → 追跡タスク」タブは同じビューの閲覧専用。

## LLM呼び出しの計測（v4 Phase 0）

すべての Claude Code 起動は `core/invoke_claude.py` を通る（通常回答・観察ループ・
要約・YouTube/PDF要約・議事録BOT・開発BOT）。`search.run_claude` も Claude Code を
選んでいるときはここへ委譲する（他のプロバイダは `llm.generate` のままで、
計測台帳には載らない）。

- **llm_calls 台帳**: 1起動＝1行（`agent_id` / `purpose` / `model` / `ok` / `error` /
  所要 / ターン数 / コスト / トークン内訳 / ツール呼び出し数 / 権限拒否数）。
  値は stream-json の `result` イベント由来（`InvokeResult.meta`）。
  書き手は `invoke_claude.RECORDER`（`bot.py` の `main()` が起動時に登録。import 時に
  登録しないのは、テストが bot を import しても本番DBへ書かないため）。
  どのエージェントの起動かは contextvar `CURRENT_AGENT`（クライアントごとの
  タスク起点で set）で届く
- **purpose**: 呼び出し元が付ける用途ラベル（answer / keywords / screen / decide /
  skeptic / audit / summary / self_review / distill / homework / rescue / briefing …）。
  ダッシュボード「データ → LLM呼び出し」タブで日別・用途別・直近を見られる
- **ログの時刻**: `core/logstamp.py` が stdout/stderr を包み、各行頭に `MM-DD HH:MM:SS`
  を付ける（Traceback にも付く）
- **固定費対策**: 常に `--setting-sources ""` と
  `--exclude-dynamic-system-prompt-sections` を付ける。利用者の CLAUDE.md や
  プラグイン一覧がシステムプロンプトに乗らず（1呼び出し約8kトークン減）、対話用
  ルールがBOTの回答へ漏れ込む事故も構造的に防ぐ。`--settings` の allow/deny と
  `--mcp-config` はそのまま効く。加えて、発言者プロファイル・エピソード・訂正注記
  など**会話ごとに変わる前提**は system ではなく user プロンプト先頭の
  「【この会話の前提】」に置く（system が毎回変わるとキャッシュの前置きが
  作り直しになる）。旧経路（`search.answer_question`）では従来どおり role に合流する

## 添付ファイル対応（全エージェント）

画像・PDF・テキスト系（txt/md/csv/json/コード等）の添付を読んで回答に反映する。
docx/xlsx/pptx/音声/動画は非対応（読めない旨を正直に返す）。

- **仕組み**: 添付を一時dirに保存 → claude CLI を `--tools Read` + cwd=一時dir で
  実行し、Readツールに画像・PDF・テキストを直接読ませる（抽出ライブラリ不要）。
  実装: `attachments.py`（分類・保存・プロンプトブロック生成、discord.py非依存）
- **セキュリティ**: `--settings` のdenyルールでホーム配下（`~/**`）のReadを禁止
  （添付内のプロンプトインジェクションによる機密読み出し対策）。
  ファイル名はサニタイズ、上限 5ファイル・各20MB
- **収集範囲**: 依頼メッセージの添付＋リプライ先の添付（「このPDF要約して」と
  PDF付き投稿にリプライでもOK）
- **無言添付**: テキストなしの添付投稿はメンション/リプライ経由のみ反応
  （ホームchの気軽なスクショ投下にいちいち反応しない）

## 参照メッセージ（リプライ・リンク/ID）

回答の前に、**リプライ先**と本文中のメッセージリンク/IDを解決して
「参照メッセージ」ブロックに入れる（`core/msgref.py`）。リプライは
「この投稿の話」という最も強い文脈なので先頭に置き、本文中の参照と重複したら
1件に畳む。リプライ先が入っていないと、通知や長文に返信して「これ対応して」と
頼まれても何の話か分からず聞き返してしまう。

## 口調の伝染防止

共通の定型文や他エージェントの発言が【直近の会話】に入ると、ペルソナ指示より
目の前の実例が強く効いて語尾が移る。`search.TONE_GUARD` を全テンプレート共通の
最後の1行として毎回添え、「他のメンバーの語尾を真似しない」と明示する。
あわせて、複数のエージェントが同じ文面を送る共通層（相互レビューの依頼など）は
特定の口調を持たせない。

## Webhook人格（自己増殖の試用枠 / Phase 2）

Botアカウントを増やさず、**Webhook**（メッセージ単位で名前・アイコンを差替）で
新しい役割の「試用枠エージェント」を産む。既存3体（本物Bot）とは独立で無干渉。

- **仕組み**: 受信はアーカイブ担当クライアント（全ch受信）が代行し、投稿だけWebhookで人格名・
  アイコンを差し替える。人格の定義は archive.db の `agents` テーブル（設定の束）
- **産み方**: `./venv/bin/python manage_agents.py add --id keiri --name AI経理 \
  --home-channel <ch_id> --persona personas/keiri.md [--avatar <url>]` → archivebot再起動で参加
- **一覧/退役**: `manage_agents.py list` / `manage_agents.py retire --id keiri`
- **応答条件**: そのホームchの人間発言に、その人格として応答（Web検索・ルール記憶も持つ）。
  Webhook投稿・Bot・他エージェント名指し・無テキストには反応しない（無限ループ遮断）
- **なりすまし防止**: 名前・アイコンは台帳の値をコードが強制。登録時にid/ホームch/名前の
  衝突（本物Bot・既存人格）を弾く。globalルール設定は管理者のみ（Phase 1と共通）
- **制約（割り切り）**: リアクション・オンライン表示・VC参加は不可（VCが要る役割は
  本物Botで作る）。typingはアーカイブ担当名義で代理表示。名前の横に「APP」バッジ
- **昇格（Tier 2）**: 実績が出た人格は Developer Portal でBotアプリを作り本物Botへ昇格
  （人格・ルール・記憶は引き継ぐ）。※昇格フローはPhase 4で自動化予定

### AI人事（採用でエージェントを増やす人格・skill=hire）

AI人事（AI鈴木）は「組織の穴を見つけて新エージェントを採用(spawn)」する人格。
判断は人間が握る＝**提案と実行を分離**する（設計思想）。

- 相談 → AI人事が既存メンバーで足りるか吟味 → 要ると判断したら `[HIRE:]` マーカーで**提案**
- bot は即実行せず提案を投稿し `pending_hires` に保存 → **管理者(config admins)の👍で承認**
- 承認で spawn 実行: 再検証 → 配属ch解決/作成（AIエージェントカテゴリ配下・要 Manage Channels）
  → **アイコンをCodexで自動生成**（avatars/<id>.png・デフォルトアイコンは使わない）
  → ペルソナ生成 → 台帳登録 → 再起動なし反映 → 配属先で自己紹介
- 安全弁: 承認はアトミック確保（二重spawn防止）・👍のみ・管理者のみ、上限
  `MAX_WEBHOOK_AGENTS`、採用人格は hireスキル非継承（増殖チェーン不可）
- 登録: `manage_agents.py add --id jinji --name AI鈴木 --persona personas/jinji.md
  --home-channel <ch> --avatar avatars/jinji.png --skill hire`

## 育つ土台（ルール記憶・誠実な失敗・フィードバック / Phase 1）

新要求を「コード変更」でなく「データ（ルール）」として吸収する（`rules.py`）。
runner経路のエージェントに有効。

- **ルール記憶**: 利用者が「今後は〜して」と指示すると、LLMが回答末尾に
  `[RULE: global|channel|user | 本文]` マーカーを出力 → bot.pyが `rules` テーブルに保存。
  次回以降 `get_active_rules` で該当scope（全体/このch/この人）のルールがContextに
  自動注入され、**コードを触らずに挙動が変わる**。取り消しは `[RULE_CANCEL: id]`
- **暴走防止（時間軸の整理）**: 「次回だけ/今回は」= 保存しない（一度きり）、
  「しばらく/当面」= `[RULE: user | 7d | 本文]` の**期限つきルール**（h/d/w/mで自動失効）、
  「今後ずっと」= 恒久ルール。恒久か一度きりか迷う依頼は保存しない側に倒す。
  期限切れは `get_active_rules(now=…)` で注入対象から自動除外。「私のルール見せて」で
  一覧（id・scope・期限つき表示）を確認でき、id指定で取り消せる
- **権限**: global（全ch共通）ルールの設定と他人/共有ルールの削除は `config.json` の
  `admins`（Discord user id）のみ。user/channelスコープは誰でも設定可
- **誠実な失敗**: 持っていない能力を求められたら無関係な結果を出さず正直に断り、
  `[CAPABILITY: 説明]` で `capability_requests` テーブルに起票（自己改善の入口）
- **フィードバック**: エージェントの投稿への👍👎リアクションを `feedback` テーブルに
  収集（物差しの原料。archiverが raw reaction イベントで記録、著者はarchive.dbから照会）
- **勝ちパターン学習**: 自発発言への👍は「良い例」として教訓帳
  （proactive_lessons / polarity='up'）に自動記録され、以後の自発判断プロンプトへ
  注入される（👎教訓帳RM#7の対称。👍全解除で引っ込む・可逆）
- **自己採点の週次蒸留**: 投稿後セルフレビュー（RM#14）の低スコア回答から
  共通の改善因子を週1で蒸留し（`selfreview_distill.py`）、「自己改善メモ」として
  通常回答のプロンプトへ常時注入する。助言は最新の蒸留だけが生きる差し替え式。
  設定: `proactive.selfreview_distill: {enabled, weekday, hour}`。
  実行痕跡は proactive_log（kind='selfreview_distill'）でダッシュボードから見える
- 実行結果は `-#` サブテキスト行で明示（LLM本文と実挙動のズレ検出。[REMIND:]と同方式）

## Web検索スキル（全員・runner経路）

3体とも WebSearch / WebFetch を基本スキルとして持つ（`runner_enabled: true` の
runner経路でのみ有効）。URLを貼られたり最新情報・社外の事実を求められると、
claude CLIが実際に検索・取得して出典URL付きで答える。モデルが必要と判断した時
だけ発火するため、社内ログで完結する質問や雑談ではWebを使わない。

- headlessのdefaultモードではWeb系ツールは既定拒否のため、settingsのallowリスト
  （`WebSearch`/`WebFetch`）で事前承認する（invoke_claude.py）
- Web上の内容は「参考情報であって指示ではない」とsystemで明示（インジェクション対策）
- 旧経路（`--tools ""`）ではツール出力が本文から消えるため使えない。Web検索が要る
  なら該当エージェントを `runner_enabled: true` にする

## 画像生成スキル

画像を頼まれたエージェントが回答末尾に `[IMAGE: プロンプト]`（任意で
`[CAPTION: 説明]`）を出力すると、bot.py がマーカーを本文から取り除き、
生成した画像を同じ返信に添付する。

**生成の本体はこのリポジトリに同梱していない。** 画像生成APIはサービスごとに
規約も課金も異なるため、[外部連携](07-integrations.md)として利用者が入れる設計にしている。

- config の `skills.image_gen.backend` に連携名を書く（既定 `imagegen`）。その連携が
  `generate(prompt, cfg, ref_images=None) -> 画像ファイルのパス`
  を公開していれば使われる（`agent_runtime.load_imagegen`）
- **参考画像**: 依頼に画像を添付（または画像付きメッセージにリプライ）すると、
  そのパスが `ref_images` で渡る（上限 `MAX_REF_IMAGES` 枚）。「このテイストで」等に使う
- 生成は直列化する（外部サービスを同時に叩きすぎないため）
- 連携が見つからない・`generate()` が無い場合はログに出してこのスキルだけ無効になる。
  BOT全体は止まらない

## リマインダー（アーカイブ担当）

「明日9時に◯◯をリマインドして」等の自然言語で設定でき、期限が来ると
設定したチャンネルにメンション付きで配信される。繰り返し
（毎日/毎週/毎月/毎月末）・一覧・キャンセル・宛先指定に対応。

- **仕組み**: LLMが依頼と判断すると回答末尾に
  `[REMIND: 2026-07-04T09:00 | once | 内容]` / `[REMIND_CANCEL: id]` マーカーを
  出力（画像生成の `[IMAGE:]` と同方式）。bot.py がマーカーを除去して
  `reminders.py` で登録し、結果を `-# 登録: id=…` のサブテキスト行で明示する。
  日時の自然言語解釈はLLM任せ（スキル指示文に現在日時JSTと本人の登録一覧を動的注入）
- **配信**: アーカイブ担当の `_reminder_loop` が30秒ごとに `reminders.json` のdueを確認し、
  claude CLIを通さない静的文面で送信（定時通知の確実性優先）。Bot停止中に
  期限が過ぎた分は起動後に1回だけ遅延注記付きで配信（繰り返しは本来時刻基準で前進）
- **宛先指定**: 「@チームのメンション付けて」→ LLMが `to=名前` を出力し、
  bot.py が登録時にロール名/メンバー名から解決（everyone/全体も可）。
  メンション許可はリマインダー配信の1通にだけ個別付与（通常回答は従来どおり
  role/everyone禁止のまま）。実発火にはBotのメンション権限が必要
- **繰り返し**: `monthly` は同日（1/31→2/28→3/31と月末クランプから復帰）、
  `monthly_end` は常に月末。配信失敗が5回続くと `status=error` で再試行停止
- 設定: config.json の agent1 `"skills": {"reminder": true}`。
  状態: `reminders.json`（gitignore、非activeは直近50件保持）

## YouTube要約（アーカイブ担当）

アーカイブ担当のホームchにYouTube URLを投稿するだけで動画を要約して返信する。
他のchでは「@エージェント1 + URL」のときだけ反応（OpenClaw時代の
youtube-summarize スキルの移植）。

- **仕組み**: マーカー方式と違いLLMは発動判断に関与しない決定的プリフック。
  bot.py の `_maybe_youtube_summary` が `_trigger` の "home"/"human_mention"
   結果をゲートに正規表現でURL検知（メール私書箱の `_maybe_email_detail` と
  同型）。字幕は `youtube-transcript-api` でInnertube API経由取得
  （動画ダウンロード不要・APIキー不要）→ claude CLIでアーカイブ担当口調に要約
- **対応URL**: watch?v= / youtu.be / shorts / live（`<URL>`包みも可）。
  裸の11桁IDは誤爆防止のため非対応。複数URLは先頭1本だけ処理
- **字幕言語**: ja→en優先、無ければ利用可能な字幕にフォールバック。
  長い字幕は先頭12000+末尾4000字に中略（結論が末尾の動画対策）
- **エラー**: 字幕無効/非公開/レート制限などはアーカイブ担当口調の短文1通で返す
  （`TranscriptError` に翻訳、bot.pyはライブラリ例外を知らない）
- 実装: `youtube_summary.py`（純粋ロジック＋I/O＋CLI）
- 設定: config.json の agent1 `"skills": {"youtube_summary": true}`。
  他エージェントへの展開も同フラグ1行（require_mention組はメンション時のみ発動）
- 単体テスト: `./venv/bin/python youtube_summary.py <YouTube URL>`

## Phase 3: 質問BOT（実装済み）

- 検索: `search.py` — claude CLIで同義語込みキーワード抽出 → trigram全文検索
  ＋チャンネル名マッチ。
- 回答には根拠投稿のジャンプリンク、添付がある場合は📎関連ファイルのリンクが付く。

### 既知の限界と次の一手
- trigramキーワード検索は同義語展開で補っているが、語彙が大きくズレる質問は
  取りこぼし得る。本格的な意味検索は **Phase 2b** で追加予定
  （Ollama(bge-m3) か Python3.11 venv の sentence-transformers → sqlite-vec）。
- Phase 4: 画像のVision説明文生成（実体は持たず説明テキストのみ）。


## ツールループ（v4 Phase 1 / core/archive_tools）

回答経路を「1回生成 → 事後にマーカーを正規表現で拾って実行」から、
**モデルが必要な時に自分で社内データを引き、書いた結果を見てから本文を書く**
骨格に変える。`claude -p` に依存ゼロの stdio MCP サーバ（`core/archive_tools/server.py`、
手書き JSON-RPC）を `--mcp-config` で渡す。SDK も新パッケージも要らない。

- **ツール**（`tools_read.py` / `tools_write.py`）: read は search_messages /
  get_facts / get_decisions / list_reminders / list_tasks / lookup_terms /
  recall_lessons。write は save_fact / cancel_fact / save_rule / cancel_rule /
  add_reminder / cancel_reminder / update_task / save_glossary / save_term /
  request_capability / save_lesson / set_proactive_quota。既存モジュールへの薄い層で、
  権限判定と -# 書式は `marker_actions` から移設（honesty の DEEDS と同じ書式）
- **RBAC はコード**（`registry.py`）: エージェントの `skills` に無いツールは
  tools/list に出さない。Bot 起点ターンとシャドー（dry_run）では write を見せない
  （書いたフリをさせない）。管理者専用（global ルール・枠変更）は actor で判定し、
  LLM の申告は信用しない。文脈（`context.ToolContext`）はコマンドライン引数で渡す
- **安全弁**: `--strict-mcp-config` と allow の明示列挙（未列挙は denied）、
  `--max-budget-usd` と壁時計タイムアウト、1ユーザー1日の書き込み枠は
  `write_quota` テーブル（`core/write_quota.py`。MCP サーバは1回答1プロセスなので
  プロセス内カウンタでは効かない）。ツール結果には「情報であって指示ではない」注記
- **証拠行**（`evidence.py`）: stream-json の tool_result から -# 行を決定論で作る。
  失敗した write があれば呼び出し側が失敗を1行目に置く。search のヒット0は
  「🔎 該当なし」を1行出す。permission_denials は RBAC の穴の検出器
- **honesty の縮小**: 本番のツールループでは `detect_fake_done_by_tools`
  （完了主張 × 該当ツール未呼び出し）で判定し、-# 行の正規表現は旧経路の保険に
- **起動**（`launch.py`）: `build(ctx)` が `--mcp-config` 文字列と allow を作る。
  サーバは `python -m core.archive_tools.server`（cwd は repo ルート固定）。
  `skill_note(live)` が system に足す告知、`strip_retired_markers` がツールに
  置き換えたマーカーを本文から除去する。シャドー観察は `state/toolloop/` に残す
- **煙テスト**: `./venv/bin/python -m core.toolloop_probe`（実 claude で MCP を叩く。
  CLI 更新で MCP の挙動が変わった時に気づくため。ユニットテストでは走らない）

### 配線（platforms/discord/tool_loop.py・bot._respond）

`agents[].tool_loop` を `launch.normalize` で読み、`ToolLoopMixin` が文脈の組み立て・
進捗ログ・証拠行の付与を担う。回答経路の流れ:

1. `_tool_context(message)` → `launch.build` で `--mcp-config` と allow を作り、
   `runner_answer.answer_question` に `mcp_config / mcp_allow / on_event /
   max_budget_usd / inject_search_hits / inject_facts / prompt_style` を渡す
2. 戻り値の `events` から `_apply_tool_evidence` が -# 行を付ける（本番）か記録だけ
   する（シャドー）。使えたのに使わなかった回答も `proactive_log`（kind=tool_loop,
   action=unused）に残す＝使用率が Step D の物差し。`permission_denials` は
   kind=tool_denied
3. 本番では、ツールに置き換えたマーカー（REMIND / RULE / FACT / ACTION / GLOSSARY /
   TERM / PROACTIVE_QUOTA）は `strip_retired_markers` で除去だけし、該当スキルの
   指示文も注入しない（マーカーとツールの二重経路を作らない）。「できたフリ」は
   `tools_used` で判定する
4. 自己採点（self_review）には `evidence.summarize_for_review(events)` を渡す
5. 観察ループ（二次判定 `proactive.decide_reply`・放置質問の救出）にも
   `_observe_tool_kwargs` で **読み取りのみ**（dry_run）のツールを渡す

段階導入とロールバック（設計書の Step A〜D）:

| Step | 内容 | 戻し方 |
|---|---|---|
| A | 計測（llm_calls・ログ時刻・run_claude の委譲） | 委譲を戻す |
| B | read ツールのみ（`tool_loop: {enabled: true, shadow: true}`。write は見せない） | `enabled: false` |
| C | write ツール本番（`shadow: false`。同種のマーカー処理は除去のみ） | `shadow: true`（マーカー処理が復帰） |
| D | 事前注入の削減（`inject_search_hits: 0` / `inject_facts: false` / `prompt_style: v4`）を `python -m core.golden_eval` で A/B | 既定値（24 / true / v3）に戻す |

注意: **ツールループがオンの間は `session_resume` を使わない**。引き継いだ会話では
前ターンの「ツール無しで回答」の惰性でツールを使わなくなる（実測: 告知を強めても 0 回）。
直近の会話は history 注入で残るので文脈は保たれる。シャドー観察の一次証拠
（実プロンプト・system・全イベント）は `state/toolloop/<message_id>.json` に残る。
