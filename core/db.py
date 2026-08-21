#!/usr/bin/env python3
"""
会話アーカイブの保存先（SQLite）。

テキストとメタデータだけを持ち、添付の実体は保存しない（IDとURLだけ記録し、
実物はプラットフォーム側のリンクで復元する）。

## プラットフォーム跨ぎの持ち方

`channels` / `users` / `messages` の `id` は **このDBの中だけで通じる整数**。
全文検索（FTS5 external content）が `content_rowid` に整数を要求するため、
主キーは整数のまま残してある。

サービス側の本当のIDは `platform` + `external_id`（どちらも文字列）に入れる。
Slack の "C01234" のような非数値IDでも、この2列があれば同じDBに同居できる。

  platform    external_id            id（rowid）
  discord     "1234567890123456789"  1234567890123456789  ← 由来がそのまま入る
  slack       "C01234"               1                    ← 連番が振られる

Discord は元のスノーフレークをそのまま id に使っている（既存データを
書き換えない）。別プラットフォームの実装は `local_id()` で id を引く。
"""

import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
-- このアーカイブ自身についての覚書。
-- いまのところ用途は1つ: **どのDiscordサーバーの記録なのか**を残すこと。
-- messages/channels は guild_id を持たないため、繋ぎ先を別サーバーへ
-- 変えても前のサーバーの会話がそのまま検索対象に残る（選んで消せない）。
-- 起動時にここと config を突き合わせて、混ざる前に止める。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    type        TEXT,
    parent_id   INTEGER,
    platform    TEXT,
    external_id TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    name         TEXT,
    display_name TEXT,
    is_bot       INTEGER DEFAULT 0,
    platform     TEXT,
    external_id  TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    channel_id  INTEGER,
    author_id   INTEGER,
    content     TEXT,
    created_at  TEXT,
    edited_at   TEXT,
    reply_to    INTEGER,
    deleted     INTEGER DEFAULT 0,
    platform    TEXT,
    external_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_messages_author  ON messages(author_id);

CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY,
    message_id    INTEGER,
    filename      TEXT,
    content_type  TEXT,
    size          INTEGER,
    is_image      INTEGER DEFAULT 0,
    is_video      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

-- 全文検索(キーワード補助)。日本語の部分一致のため trigram トークナイザを使用
-- （検索語は3文字以上が必要。意味検索はPhase2のベクトルが担う）。
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);

-- チャンネル文脈要約（エージェントv2 Phase 0）。連続性は生ログではなく
-- 「要約1本」で担保する。設計: docs/agents-v2-design.md §3
CREATE TABLE IF NOT EXISTS thread_summaries (
    channel_id                INTEGER PRIMARY KEY,
    summary                   TEXT,
    covered_until_message_id  INTEGER,
    updated_at                TEXT
);

-- 育つ土台（エージェントv2 Phase 1）。設計: docs/agents-v2-design.md §5
-- 新要求をコードでなくデータとして吸収する。scope=global/channel:<id>/user:<id>
-- expires_at: 期限つきルールの失効時刻（naive JST文字列）。NULLは恒久。
-- 「次回だけ=リマインダー / 当面=期限つき / ずっと=恒久」の時間軸の一部
CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT,
    scope         TEXT,
    rule_text     TEXT,
    created_by    TEXT,
    source_msg_id INTEGER,
    active        INTEGER DEFAULT 1,
    created_at    TEXT,
    expires_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rules_lookup ON rules(agent_id, active);

-- ツール台帳（エージェントv2 Phase 3）。builderが生やしたプラグインの記録。
CREATE TABLE IF NOT EXISTS tool_registry (
    name        TEXT PRIMARY KEY,
    marker      TEXT,
    source_req  INTEGER,
    version     INTEGER DEFAULT 1,
    status      TEXT DEFAULT 'active',
    created_at  TEXT,
    updated_at  TEXT
);

-- 誠実な失敗の出口＝自己改善の入口。無い能力を発明せず起票する
CREATE TABLE IF NOT EXISTS capability_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT,
    description   TEXT,
    context       TEXT,
    requested_by  TEXT,
    source_msg_id INTEGER,
    status        TEXT DEFAULT 'open',
    created_at    TEXT
);

-- 物差しの原料（適応度関数の元データ）。👍👎リアクションを集計する
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER,
    agent_id    TEXT,
    kind        TEXT,
    value       TEXT,
    user_id     TEXT,
    created_at  TEXT,
    UNIQUE(message_id, user_id, value)
);

-- AI人事の採用提案（承認待ちキュー）。管理者の👍で承認→spawn実行。
-- 判断（誰を採るか）は人間が握る＝提案と実行を分離する（設計思想メモ）。
CREATE TABLE IF NOT EXISTS pending_hires (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    INTEGER,
    new_id        TEXT,
    name          TEXT,
    role          TEXT,
    channel_name  TEXT,
    channel_id    INTEGER,
    status        TEXT DEFAULT 'pending',
    proposed_by   TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_hires_msg ON pending_hires(message_id);

-- AI人事の解雇提案（承認待ち）。採用と対称。管理者の👍でretire実行。
CREATE TABLE IF NOT EXISTS pending_fires (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    INTEGER,
    target_id     TEXT,
    target_name   TEXT,
    status        TEXT DEFAULT 'pending',
    proposed_by   TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_fires_msg ON pending_fires(message_id);

-- 自己増殖の台帳（エージェントv2 Phase 2）。設計: docs/agents-v2-design.md §3,§7
-- Webhook人格（試用枠）の「設定の束」。既存3体（本物Bot）はconfig.jsonのまま。
-- 新しい役割はここに1行足すだけで即日参加する（コード追加ゼロ）。
CREATE TABLE IF NOT EXISTS agents (
    id                   TEXT PRIMARY KEY,
    kind                 TEXT DEFAULT 'webhook',
    name                 TEXT,
    avatar_url           TEXT,
    home_channel_id      INTEGER,
    persona_file         TEXT,
    skills_json          TEXT,
    allowed_tools_json   TEXT,
    status               TEXT DEFAULT 'active',
    created_at           TEXT,
    home_channel_created INTEGER DEFAULT 0
);

-- 開発BOT(開発BOT)の改修ジョブ台帳（Phase 2）。起票(capability_requests)を1件受け
-- worktreeで実装→テスト→👍承認→deploy の状態を追う。
-- status: building→built→approved→deployed / rejected / failed
CREATE TABLE IF NOT EXISTS dev_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cap_req_id    INTEGER,
    branch        TEXT,
    worktree      TEXT,
    status        TEXT DEFAULT 'building',
    channel_id    INTEGER,
    message_id    INTEGER,
    summary       TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_dev_jobs_status ON dev_jobs(status);
CREATE INDEX IF NOT EXISTS idx_dev_jobs_msg ON dev_jobs(message_id);

-- 開発BOTの教訓帳。失敗理由・👎却下の理由を貯め、次の改修プロンプトへ注入する
-- ＝セッション使い捨てでもジョブをまたいで学習する（自己改善ループの記憶）。
CREATE TABLE IF NOT EXISTS dev_lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cap_req_id  INTEGER,
    job_id      INTEGER,
    kind        TEXT,
    text        TEXT,
    created_at  TEXT
);

-- 自発性の層（エージェントv3 Phase A）。設計: docs/agents-v3-proactive.md
-- 観察ループのチェックポイント（エージェントごとに前回どこまで見たか）
CREATE TABLE IF NOT EXISTS proactive_state (
    agent_id                 TEXT PRIMARY KEY,
    last_checked_message_id  INTEGER,
    last_run_at              TEXT
);

-- 自発発言の台帳。発言(spoke)だけでなく沈黙(silent)も記録する
-- （黙っていた実績が観測できて初めて信頼になる。週次レポート・日次枠の原料）
CREATE TABLE IF NOT EXISTS proactive_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT,
    kind                TEXT,
    action              TEXT,
    channel_id          INTEGER,
    trigger_message_id  INTEGER,
    posted_message_id   INTEGER,
    detail              TEXT,
    created_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_proactive_log_agent
    ON proactive_log(agent_id, action, created_at);

-- 議事録の納期追跡（エージェントv3 Phase B）。設計: docs/agents-v3-proactive.md §4
-- 議事録から抽出した担当者・期日つきTODO。声かけ段階は none→before→day→overdue
CREATE TABLE IF NOT EXISTS action_items (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id               TEXT,
    source_message_id      INTEGER,
    channel_id             INTEGER,
    confirm_message_id     INTEGER,
    task                   TEXT,
    owners                 TEXT,
    due_date               TEXT,
    urgent                 INTEGER DEFAULT 0,
    status                 TEXT DEFAULT 'open',
    nudge_stage            TEXT DEFAULT 'none',
    last_nudge_message_id  INTEGER,
    created_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_items_status ON action_items(status);

-- 宿題検出（自己コミットの追跡 / エージェントv3 Phase E）。設計: docs/agents-v3-proactive.md
-- 会話中の「あとでやる」「確認しとく」等の自己コミットを検知し、数日後に本人へ一度だけ
-- 「あれどうなりました?」と声かけする。status: open→asked（声かけ済・終端）/expired/dropped。
-- 検知は沈黙（追跡のみ）。外向きは声かけだけで、フラグ＋シャドーで安全化する。
CREATE TABLE IF NOT EXISTS homework_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id             TEXT,
    source_message_id    INTEGER,
    channel_id           INTEGER,
    owner                TEXT,
    task                 TEXT,
    committed_date       TEXT,
    follow_up_date       TEXT,
    status               TEXT DEFAULT 'open',
    followup_message_id  INTEGER,
    created_at           TEXT,
    UNIQUE(agent_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_homework_status ON homework_items(status);

-- 自発発言の枠（日次上限）の上書き（v3 Phase D）。無ければconfigの既定値。
-- 「もっと言っていいよ」の会話→マーカーで更新される（管理者のみ・コードで執行）
CREATE TABLE IF NOT EXISTS proactive_settings (
    agent_id     TEXT PRIMARY KEY,
    daily_quota  INTEGER,
    updated_at   TEXT
);

-- 進化ロードマップ（100案のバックログ）。開発BOTがAI開発室へカードを1枚ずつ投稿し
-- 👍=実装（route=devbot は起票化・session は管理者セッション行き）👎=見送り。
-- status: pending→proposed→approved/queued_session/skipped
CREATE TABLE IF NOT EXISTS roadmap_items (
    id               INTEGER PRIMARY KEY,
    title            TEXT,
    description      TEXT,
    category         TEXT,
    tier             TEXT,
    route            TEXT,
    effect           INTEGER,
    cost             INTEGER,
    status           TEXT DEFAULT 'pending',
    card_message_id  INTEGER,
    cap_req_id       INTEGER,
    created_at       TEXT,
    decided_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_roadmap_status ON roadmap_items(status);

-- デプロイ履歴（!revert とデプロイ後カナリア監視の台帳）。
-- canary_status: watching→ok（24h無事）/alerted（エラー急増を通知済み）
CREATE TABLE IF NOT EXISTS deploy_history (
    job_id           INTEGER PRIMARY KEY,
    cap_req_id       INTEGER,
    pre_sha          TEXT,
    post_sha         TEXT,
    files            TEXT,
    deployed_at      TEXT,
    reverted_at      TEXT,
    canary_baseline  INTEGER,
    canary_status    TEXT DEFAULT 'watching'
);
CREATE INDEX IF NOT EXISTS idx_deploy_history_cap ON deploy_history(cap_req_id);

-- 起票の自動拾い上げ（進化ロードマップ#21）。エージェントのオーガニック起票を
-- 開発BOTがAI開発室へ提案し、👍=着手/👎=見送り（起票をrejectedで閉じる）。
-- checkpoint は proactive_state（agent_id='capwatch:devbot'）が持つ。
CREATE TABLE IF NOT EXISTS cap_proposals (
    cap_req_id  INTEGER PRIMARY KEY,
    message_id  INTEGER,
    status      TEXT DEFAULT 'proposed',
    created_at  TEXT,
    decided_at  TEXT
);

-- 単語帳（進化ロードマップ#5）。誤変換・誤記の「誤→正」ペア。回答・議事録・
-- 検索に決定論で適用され、登録時に決定台帳・納期タスクの既存行も遡及修正する。
CREATE TABLE IF NOT EXISTS glossary (
    wrong       TEXT PRIMARY KEY,
    correct     TEXT,
    created_by  TEXT,
    created_at  TEXT
);

-- 固有名詞辞書。正式表記（＋任意の説明）をユーザーが登録する。正誤表と違い
-- 「音が近い未知の誤変換」もLLMがこの正式表記に寄せられる（議事録・回答で参照）。
CREATE TABLE IF NOT EXISTS terms (
    term        TEXT PRIMARY KEY,
    description TEXT,
    created_by  TEXT,
    created_at  TEXT
);

-- 人物プロファイル自動蓄積（進化ロードマップ#1）。会話からメンバーごとの
-- 役割・得意分野・好み・注意点を蒸留し、応答のContextに注入して個別最適化する。
-- 静かなデータ（更新は投稿を伴わない）。thread_summaries と同じ統合更新方式。
CREATE TABLE IF NOT EXISTS profiles (
    user_id                   INTEGER PRIMARY KEY,
    display_name              TEXT,
    profile                   TEXT,
    covered_until_message_id  INTEGER,
    updated_at                TEXT
);

-- イベント逆算スケジュール（進化ロードマップ#35）。決定台帳から開催日つきの
-- イベントを検知→逆算案を提案→管理者✅で action_items（納期追跡）へ載せる。
-- status: proposed（✅❌待ち）/ planned / dismissed / shadow
CREATE TABLE IF NOT EXISTS events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id             TEXT,
    name                 TEXT,
    event_date           TEXT,
    source_decision_id   INTEGER UNIQUE,
    channel_id           INTEGER,
    milestones_json      TEXT,
    proposal_message_id  INTEGER,
    status               TEXT DEFAULT 'proposed',
    created_at           TEXT
);

-- ゴールデンセット（進化ロードマップ#16）。👍がついた実Q&Aを評価セットとして
-- 自動蓄積する。将来のプロンプト/モデル変更時の回帰テスト資産（検証済み置換の土台）。
CREATE TABLE IF NOT EXISTS golden_set (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id          TEXT,
    question          TEXT,
    answer            TEXT,
    source_answer_id  INTEGER UNIQUE,
    channel_id        INTEGER,
    created_at        TEXT
);

-- ルール棚卸しの提案（進化ロードマップ#2）。週次でアーカイブ担当が重複・陳腐化を提案し
-- 管理者✅で無効化を実行する。status: proposed/applied/dismissed
CREATE TABLE IF NOT EXISTS rule_reviews (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_json         TEXT,
    proposal_message_id  INTEGER,
    status               TEXT DEFAULT 'proposed',
    created_at           TEXT
);

-- 自動化アイデアの提案（進化ロードマップ#24）。👍で起票→開発BOTの自動拾い上げへ。
CREATE TABLE IF NOT EXISTS auto_proposals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_json         TEXT,
    proposal_message_id  INTEGER,
    status               TEXT DEFAULT 'proposed',
    created_at           TEXT
);

-- エピソード記憶（進化ロードマップ#3）。チャンネル＝プロジェクト単位の
-- 出来事タイムライン。thread_summaries が「今の文脈」なのに対し、こちらは
-- 「何がいつ起きたか」の履歴（決定・完了・イベントを時系列で積む）。
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER,
    happened_on TEXT,
    kind        TEXT,
    summary     TEXT,
    source_ref  INTEGER,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_ch ON episodes(channel_id, id);

-- A/Bプロンプト実験（進化ロードマップ#12）。判定プロンプトの変種ごとに
-- 使用回数と👍👎を貯め、十分な差がついたら人間へ採用提案する。
CREATE TABLE IF NOT EXISTS prompt_variants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slot        TEXT,
    variant     TEXT,
    body        TEXT,
    used        INTEGER DEFAULT 0,
    up          INTEGER DEFAULT 0,
    down        INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  TEXT,
    UNIQUE(slot, variant)
);

-- 予言の封筒（進化ロードマップ#94）。月初に予測を封印し翌月に答え合わせする。
-- 外しても実害が無い形で「予測能力が使い物になるか」を測る実験装置。
CREATE TABLE IF NOT EXISTS prophecies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT,
    period       TEXT,
    payload_json TEXT,
    verdict_json TEXT,
    status       TEXT DEFAULT 'sealed',
    created_at   TEXT,
    opened_at    TEXT
);

-- エージェント間勉強会（進化ロードマップ#61）。個体が学んだルールを
-- 全体へ広げる提案。✅でscope=globalへ昇格（判断は人間）。
CREATE TABLE IF NOT EXISTS share_proposals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id              INTEGER,
    from_agent           TEXT,
    proposal_message_id  INTEGER,
    status               TEXT DEFAULT 'proposed',
    created_at           TEXT
);

-- 決定の波及チェッカー（とっておき#101）。新しい決定が入ったとき、
-- 影響を受ける既存の記録（旧決定・タスク・リマインダー・イベント）を提案し、
-- ✅で矛盾する旧決定をsupersededにする。
CREATE TABLE IF NOT EXISTS ripple_proposals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id          INTEGER,
    impacts_json         TEXT,
    proposal_message_id  INTEGER,
    status               TEXT DEFAULT 'proposed',
    created_at           TEXT
);

-- 生きた社内Wiki（とっておき#103）。トピックごとの「今の正」を1本の投稿に
-- 編纂し、関連する新決定が入ったら編集で更新し続ける（チャットを流さない）。
CREATE TABLE IF NOT EXISTS wiki_pages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    topic            TEXT UNIQUE,
    channel_id       INTEGER,
    message_id       INTEGER,
    last_decision_id INTEGER DEFAULT 0,
    created_by       TEXT,
    created_at       TEXT,
    updated_at       TEXT
);

-- 会話セッションの持続（resume方式カナリア・2026-08-03）。プロセスは
-- 使い捨てのまま、(エージェント, チャンネル)単位で claude の会話セッションを
-- 有界に継続する（ホットウィンドウ・ターン上限・世代上限はコードで執行）。
CREATE TABLE IF NOT EXISTS agent_sessions (
    agent_id     TEXT,
    channel_id   INTEGER,
    session_id   TEXT,
    turns        INTEGER DEFAULT 0,
    started_at   TEXT,
    last_used_at TEXT,
    PRIMARY KEY (agent_id, channel_id)
);

-- エージェント横断のトリガー排他（2026-08-07・二重反応対策）。
-- 自発発言・引き継ぎは投稿前に対象メッセージidをクレームし、先勝ちで排他する
-- （アーカイブ担当とデザイン担当が同じ発言に別々に反応してしまう事故の根絶）。
CREATE TABLE IF NOT EXISTS proactive_claims (
    trigger_message_id INTEGER PRIMARY KEY,
    agent_id           TEXT,
    kind               TEXT,
    created_at         TEXT
);

-- シート期日ウォッチ（起票#11・2026-08-07）。監視タブから抽出した期日と
-- 通知段階（0=未通知/1=2日前済/2=当日済）。配信は観察ループが安いSQLで行う。
CREATE TABLE IF NOT EXISTS sheet_deadlines (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    alias             TEXT,
    tab               TEXT,
    item_key          TEXT,
    name              TEXT,
    due_date          TEXT,
    channel_id        INTEGER,
    stage             INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'active',
    batch_message_id  INTEGER,
    created_at        TEXT,
    updated_at        TEXT,
    UNIQUE(alias, tab, item_key)
);

-- 監視タブの内容ハッシュ（変化がない周期はLLMを起動しない）。
CREATE TABLE IF NOT EXISTS sheet_watch_state (
    alias        TEXT,
    tab          TEXT,
    content_hash TEXT,
    updated_at   TEXT,
    PRIMARY KEY (alias, tab)
);

-- 事実台帳（2026-08-18）。決定台帳が「決めたこと」なのに対し、こちらは
-- 「いま実際どうなっているか」を保持する。人間の訂正・状況説明の受け皿で、
-- これが無かったため「認識更新するっス」と言っても書き込む先が存在せず
-- 口約束で終わっていた（グッズ納期のすれ違い事例）。
-- 同じ主題(topic)の古い事実は superseded にして最新だけを active に保つ。
CREATE TABLE IF NOT EXISTS facts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id           TEXT,
    topic              TEXT,
    fact               TEXT,
    source_kind        TEXT,
    source_message_id  INTEGER,
    channel_id         INTEGER,
    stated_by          TEXT,
    status             TEXT DEFAULT 'active',
    created_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status, topic);

-- 未回答質問の救済（進化ロードマップ#31）。24時間放置された質問を判定・救済した
-- 記録（重複判定の防止台帳）。status: rescued / shadow / skipped_answered
CREATE TABLE IF NOT EXISTS rescues (
    message_id         INTEGER PRIMARY KEY,
    agent_id           TEXT,
    status             TEXT,
    posted_message_id  INTEGER,
    created_at         TEXT
);

-- 自発発言の教訓帳（進化ロードマップ#7）。👎がついた自発発言の内容を自動記録し、
-- 二次判定プロンプトへ注入＝「言い方・中身」を学習する（#11の型抑制と対）。
-- message_id UNIQUE: 同じ投稿への複数👎で教訓が重複しない。👎全解除で inactive。
CREATE TABLE IF NOT EXISTS proactive_lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT,
    kind        TEXT,
    channel_id  INTEGER,
    message_id  INTEGER UNIQUE,
    text        TEXT,
    active      INTEGER DEFAULT 1,
    created_at  TEXT
);

-- 決定事項台帳（進化ロードマップ#4）。議事録の✅決定と会話中の明確な決定を
-- 蓄積し、自発発言の裏取り（①矛盾指摘・③確実情報・④想起）の一次資料にする。
-- 静かなデータ: 台帳への記録自体は新しい投稿を増やさない。
CREATE TABLE IF NOT EXISTS decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id           TEXT,
    decision           TEXT,
    topic              TEXT,
    source_kind        TEXT,
    source_message_id  INTEGER,
    channel_id         INTEGER,
    decided_on         TEXT,
    status             TEXT DEFAULT 'active',
    created_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);

-- スプレッドシート台帳（起票#3）。管理者が登録したシートだけを
-- 別名（alias）で読み書きできる。docs/sheets-design.md
CREATE TABLE IF NOT EXISTS sheet_registry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alias          TEXT,               -- LLM/人間が使う呼び名（言語境界の要）
    spreadsheet_id TEXT,               -- Google側ID（LLMには渡さない）
    title          TEXT,               -- 登録時に取得した実タイトル
    mode           TEXT DEFAULT 'rw',  -- 'read' | 'rw'
    agent_id       TEXT,               -- 使えるエージェント
    watch          TEXT,               -- Phase 2: 期日ウォッチ設定JSON
    registered_by  TEXT,
    created_at     TEXT,
    active         INTEGER DEFAULT 1
);
-- aliasはエージェント内で一意（activeなもののみ。解除→再登録を許す）
CREATE UNIQUE INDEX IF NOT EXISTS idx_sheet_alias
    ON sheet_registry(agent_id, alias) WHERE active=1;
"""


#: 由来不明の既存データに割り当てるプラットフォーム名。
#: このDBは元々 Discord 専用だったので、既存行は全部 Discord 由来と見なす。
LEGACY_PLATFORM = "discord"

#: platform / external_id を持たせるテーブル
_EXTERNAL_ID_TABLES = ("channels", "users", "messages")


def _migrate_platform_columns(conn):
    """platform / external_id を足して埋め、一意索引を張る（冪等）。

    索引をSCHEMA側で作らないのは、既存DBにはまだ列が無いから
    （CREATE TABLE IF NOT EXISTS は既存テーブルに列を足さない）。
    列を足したここで作るのが唯一正しい順序。

    元のDBは Discord 専用で、id にスノーフレークがそのまま入っていた。
    その値を external_id へ写して platform='discord' を立てるだけなので、
    id は1つも動かない＝全文検索の張り直しも不要。
    """
    for table in _EXTERNAL_ID_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not cols:
            continue  # テーブルがまだ無い（新規DBは SCHEMA 側で作られる）
        if "platform" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN platform TEXT")
        if "external_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN external_id TEXT")
        # 未設定の行だけ埋める（2回目以降は0行）
        conn.execute(
            f"""UPDATE {table}
                   SET platform = ?, external_id = CAST(id AS TEXT)
                 WHERE platform IS NULL""",
            (LEGACY_PLATFORM,),
        )
        conn.execute(
            f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_external
                    ON {table}(platform, external_id)""")


def local_id(conn, table, platform, external_id):
    """サービス側のID → このDBの id を引く（無ければ None）。

    Discord は id にスノーフレークをそのまま使っているので、この関数を
    通さなくても動く。**別プラットフォームの実装は必ずここを通すこと**
    （"C01234" のような非数値IDは id に入れられないため）。
    """
    if table not in _EXTERNAL_ID_TABLES:
        raise ValueError(f"external_id を持たないテーブルです: {table}")
    row = conn.execute(
        f"SELECT id FROM {table} WHERE platform=? AND external_id=?",
        (platform, str(external_id)),
    ).fetchone()
    return row[0] if row else None


def _migrate(conn):
    """既存DBへの追加カラム等の後方互換マイグレーション（冪等）。"""
    _migrate_platform_columns(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rules)")]
    if "expires_at" not in cols:
        # Phase 1で作った既存rulesテーブルにexpires_atを足す
        conn.execute("ALTER TABLE rules ADD COLUMN expires_at TEXT")
    acols = [r[1] for r in conn.execute("PRAGMA table_info(agents)")]
    if acols and "home_channel_created" not in acols:
        # AI人事が採用時に作ったチャンネルか（解雇時に削除するか判断）
        conn.execute(
            "ALTER TABLE agents ADD COLUMN home_channel_created INTEGER DEFAULT 0")
    lcols = [r[1] for r in conn.execute("PRAGMA table_info(proactive_lessons)")]
    if lcols and "polarity" not in lcols:
        # RM#7の教訓帳を極性つきに拡張: down=👎の失敗例（従来） /
        # up=👍の勝ちパターン / advice=自己採点の蒸留（メッセージ非紐付け）
        conn.execute(
            "ALTER TABLE proactive_lessons ADD COLUMN polarity TEXT DEFAULT 'down'")


#: meta テーブルのキー: このアーカイブが記録しているDiscordサーバー
ARCHIVE_GUILD_KEY = "archive_guild_id"


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def set_meta(conn, key, value):
    conn.execute(
        """INSERT INTO meta(key, value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, None if value is None else str(value)))


def archived_guild_id(conn):
    """このアーカイブが記録しているサーバーID（未記録なら None）。"""
    return get_meta(conn, ARCHIVE_GUILD_KEY)


def remember_archive_guild(conn, guild_id):
    """記録対象のサーバーを覚える。初回のアーカイブ時に1度だけ書かれる。"""
    set_meta(conn, ARCHIVE_GUILD_KEY, guild_id)


def init_db(path):
    """スキーマを作成（冪等）＋マイグレーション。"""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 書き込み競合（要約更新・回答・ルール保存の同時書込）で即エラーに
    # せず少し待つ。1プロセスだが to_thread で別スレッド書込があるため
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_channel(conn, *, id, name, type, parent_id=None,
                   platform=LEGACY_PLATFORM, external_id=None):
    """チャンネルを保存。external_id 未指定なら id をそのまま採用する
    （Discord のように id がサービス側IDと同じ場合）。"""
    conn.execute(
        """INSERT INTO channels(id, name, type, parent_id, platform, external_id)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type,
                                         parent_id=excluded.parent_id,
                                         platform=excluded.platform,
                                         external_id=excluded.external_id""",
        (id, name, type, parent_id, platform,
         str(external_id if external_id is not None else id)),
    )


def upsert_user(conn, *, id, name, display_name, is_bot=False,
                platform=LEGACY_PLATFORM, external_id=None):
    conn.execute(
        """INSERT INTO users(id, name, display_name, is_bot, platform, external_id)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                                         display_name=excluded.display_name,
                                         is_bot=excluded.is_bot,
                                         platform=excluded.platform,
                                         external_id=excluded.external_id""",
        (id, name, display_name, 1 if is_bot else 0, platform,
         str(external_id if external_id is not None else id)),
    )


def _fts_reindex(conn, id, new_content):
    """external content FTS を正しく張り替える。旧内容を 'delete' で消してから
    新内容を挿入する（この順序でないと旧タームがインデックスに残留して
    検索が偽陽性を蓄積する。FTS5 external content の仕様）。"""
    old = conn.execute("SELECT content FROM messages WHERE id=?", (id,)).fetchone()
    if old is not None:
        conn.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, content) "
            "VALUES('delete', ?, ?)", (id, old[0] or ""))
    conn.execute("INSERT INTO messages_fts(rowid, content) VALUES(?,?)",
                 (id, new_content or ""))


def insert_message(conn, *, id, channel_id, author_id, content,
                   created_at, edited_at=None, reply_to=None,
                   platform=LEGACY_PLATFORM, external_id=None):
    """メッセージを保存。既存IDは上書き（バックフィルと再起動の重複を吸収）。"""
    _fts_reindex(conn, id, content)   # messagesをUPDATEする前に旧FTSを消す
    conn.execute(
        """INSERT INTO messages(id, channel_id, author_id, content,
                                created_at, edited_at, reply_to, deleted,
                                platform, external_id)
           VALUES(?,?,?,?,?,?,?,0,?,?)
           ON CONFLICT(id) DO UPDATE SET content=excluded.content,
                                         edited_at=excluded.edited_at,
                                         deleted=0,
                                         platform=excluded.platform,
                                         external_id=excluded.external_id""",
        (id, channel_id, author_id, content, created_at, edited_at, reply_to,
         platform, str(external_id if external_id is not None else id)),
    )


def insert_attachment(conn, *, id, message_id, filename, content_type, size,
                      is_image=False, is_video=False):
    conn.execute(
        """INSERT INTO attachments(id, message_id, filename, content_type, size,
                                   is_image, is_video)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO NOTHING""",
        (id, message_id, filename, content_type, size,
         1 if is_image else 0, 1 if is_video else 0),
    )


def mark_deleted(conn, message_id):
    # 論理削除。FTSからも旧タームを除去（search側のdeleted=0で隠れるが
    # インデックス肥大を防ぐ）
    row = conn.execute("SELECT content FROM messages WHERE id=?",
                       (message_id,)).fetchone()
    if row is not None:
        conn.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, content) "
            "VALUES('delete', ?, ?)", (message_id, row[0] or ""))
    conn.execute("UPDATE messages SET deleted=1 WHERE id=?", (message_id,))


def update_content(conn, *, id, content, edited_at):
    _fts_reindex(conn, id, content)   # messagesをUPDATEする前に旧FTSを消す
    conn.execute("UPDATE messages SET content=?, edited_at=? WHERE id=?",
                 (content, edited_at, id))


def last_message_id(conn, channel_id):
    """チャンネルの保存済み最大message_id。バックフィル再開の起点に使う。"""
    row = conn.execute(
        "SELECT MAX(id) FROM messages WHERE channel_id=?", (channel_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def get_thread_summary(conn, channel_id):
    """チャンネルの文脈要約。無ければ None。"""
    row = conn.execute(
        """SELECT summary, covered_until_message_id, updated_at
           FROM thread_summaries WHERE channel_id=?""",
        (channel_id,),
    ).fetchone()
    if row is None:
        return None
    return {"summary": row[0], "covered_until": row[1], "updated_at": row[2]}


def upsert_thread_summary(conn, *, channel_id, summary, covered_until,
                          updated_at):
    conn.execute(
        """INSERT INTO thread_summaries(channel_id, summary,
                                        covered_until_message_id, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(channel_id) DO UPDATE SET
               summary=excluded.summary,
               covered_until_message_id=excluded.covered_until_message_id,
               updated_at=excluded.updated_at""",
        (channel_id, summary, covered_until, updated_at),
    )


def _summary_row(r):
    return {"id": r[0], "author": r[1] or "?", "is_bot": bool(r[2]),
            "content": r[3] or "", "created_at": r[4]}


def messages_after(conn, channel_id, after_id=None, limit=80):
    """チャンネルの after_id より新しいメッセージを古い順で返す（要約更新用）。

    古い側から limit 件を返す: 新規が limit を超えても次回更新で続きから
    消化され、取りこぼしが出ない（covered_until は段階的に前進する）。"""
    rows = conn.execute(
        """SELECT m.id, u.display_name, u.is_bot, m.content, m.created_at
           FROM messages m LEFT JOIN users u ON u.id=m.author_id
           WHERE m.channel_id=? AND m.deleted=0
             AND (? IS NULL OR m.id > ?)
           ORDER BY m.id ASC LIMIT ?""",
        (channel_id, after_id, after_id, limit),
    ).fetchall()
    return [_summary_row(r) for r in rows]


def latest_messages(conn, channel_id, limit=80):
    """チャンネルの最新 limit 件を古い順で返す（要約のコールドスタート用）。"""
    rows = conn.execute(
        """SELECT m.id, u.display_name, u.is_bot, m.content, m.created_at
           FROM messages m LEFT JOIN users u ON u.id=m.author_id
           WHERE m.channel_id=? AND m.deleted=0
           ORDER BY m.id DESC LIMIT ?""",
        (channel_id, limit),
    ).fetchall()
    rows.reverse()
    return [_summary_row(r) for r in rows]


# ---------------------------------------------------------------- rules (Phase 1)

def add_rule(conn, *, agent_id, scope, rule_text, created_by,
             source_msg_id, created_at, expires_at=None):
    """ルールを1件保存し、採番されたidを返す。expires_atはNULLで恒久。"""
    cur = conn.execute(
        """INSERT INTO rules(agent_id, scope, rule_text, created_by,
                             source_msg_id, active, created_at, expires_at)
           VALUES(?,?,?,?,?,1,?,?)""",
        (agent_id, scope, rule_text, created_by, source_msg_id, created_at,
         expires_at),
    )
    return cur.lastrowid


def get_active_rules(conn, agent_id, scopes, now=None):
    """agent_id の active なルールを scope 集合で絞って古い順に返す。
    now（naive JST文字列）を渡すと、期限切れ（expires_at <= now）を除外する。
    expires_at と now は同じISO風フォーマットなので辞書順比較で正しい。"""
    if not scopes:
        return []
    placeholders = ",".join("?" * len(scopes))
    sql = (f"""SELECT id, scope, rule_text, expires_at FROM rules
               WHERE agent_id=? AND active=1 AND scope IN ({placeholders})""")
    params = [agent_id, *scopes]
    if now is not None:
        sql += " AND (expires_at IS NULL OR expires_at > ?)"
        params.append(now)
    sql += " ORDER BY id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [{"id": r[0], "scope": r[1], "rule_text": r[2],
             "expires_at": r[3]} for r in rows]


def get_rule(conn, rule_id, agent_id):
    """active なルール1件を返す（無ければ None）。所有者チェック等に使う。"""
    row = conn.execute(
        """SELECT id, scope, rule_text, created_by, expires_at FROM rules
           WHERE id=? AND agent_id=? AND active=1""",
        (rule_id, agent_id),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "scope": row[1], "rule_text": row[2],
            "created_by": row[3], "expires_at": row[4]}


def deactivate_rule(conn, rule_id, agent_id):
    """ルールを無効化。対象があれば rule_text を返す（無ければ None）。
    権限チェックは呼び出し側（bot._apply_rule_markers）が get_rule で行う。"""
    row = conn.execute(
        "SELECT rule_text FROM rules WHERE id=? AND agent_id=? AND active=1",
        (rule_id, agent_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE rules SET active=0 WHERE id=?", (rule_id,))
    return row[0]


def register_tool(conn, *, name, marker, source_req, created_at):
    """ツールを台帳に登録（再登録はversion++で更新）。"""
    conn.execute(
        """INSERT INTO tool_registry(name, marker, source_req, version,
               status, created_at, updated_at)
           VALUES(?,?,?,1,'active',?,?)
           ON CONFLICT(name) DO UPDATE SET
               marker=excluded.marker, source_req=excluded.source_req,
               version=tool_registry.version+1, status='active',
               updated_at=excluded.updated_at""",
        (name, marker, source_req, created_at, created_at),
    )


def get_capability_request(conn, req_id):
    """能力起票1件（builderの入力）。無ければ None。"""
    row = conn.execute(
        """SELECT id, agent_id, description, context, status
           FROM capability_requests WHERE id=?""", (req_id,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "agent_id": row[1], "description": row[2],
            "context": row[3], "status": row[4]}


def set_capability_status(conn, req_id, status):
    conn.execute("UPDATE capability_requests SET status=? WHERE id=?",
                 (status, req_id))


def list_tools(conn):
    rows = conn.execute(
        "SELECT name, marker, version, status FROM tool_registry "
        "ORDER BY name").fetchall()
    return [{"name": r[0], "marker": r[1], "version": r[2], "status": r[3]}
            for r in rows]


def add_capability_request(conn, *, agent_id, description, context,
                           requested_by, source_msg_id, created_at):
    """能力不足の起票（誠実な失敗の出口）。idを返す。"""
    cur = conn.execute(
        """INSERT INTO capability_requests(agent_id, description, context,
               requested_by, source_msg_id, status, created_at)
           VALUES(?,?,?,?,?,'open',?)""",
        (agent_id, description, context, requested_by, source_msg_id,
         created_at),
    )
    return cur.lastrowid


# ------------------------------------------------------------ dev_jobs (Phase 2)

_DEV_JOB_COLS = ("id", "cap_req_id", "branch", "worktree", "status",
                 "channel_id", "message_id", "summary", "created_at",
                 "updated_at")


def _dev_job_row(row):
    return dict(zip(_DEV_JOB_COLS, row)) if row else None


def add_dev_job(conn, *, cap_req_id, branch, worktree, channel_id, created_at):
    """改修ジョブを起票（status=building）。idを返す。"""
    cur = conn.execute(
        """INSERT INTO dev_jobs(cap_req_id, branch, worktree, status,
               channel_id, created_at, updated_at)
           VALUES(?,?,?,'building',?,?,?)""",
        (cap_req_id, branch, worktree, channel_id, created_at, created_at))
    return cur.lastrowid


def get_dev_job(conn, job_id):
    return _dev_job_row(conn.execute(
        f"SELECT {','.join(_DEV_JOB_COLS)} FROM dev_jobs WHERE id=?",
        (job_id,)).fetchone())


def get_dev_job_by_message(conn, message_id):
    """承認リアクションの対象ジョブを引く（Phase 3で使用）。"""
    return _dev_job_row(conn.execute(
        f"SELECT {','.join(_DEV_JOB_COLS)} FROM dev_jobs WHERE message_id=?",
        (message_id,)).fetchone())


def update_dev_job(conn, job_id, *, updated_at, status=None, message_id=None,
                   summary=None):
    """status/message_id/summary を部分更新（Noneの項目は据え置き）。"""
    sets, vals = ["updated_at=?"], [updated_at]
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if message_id is not None:
        sets.append("message_id=?"); vals.append(message_id)
    if summary is not None:
        sets.append("summary=?"); vals.append(summary)
    vals.append(job_id)
    conn.execute(f"UPDATE dev_jobs SET {', '.join(sets)} WHERE id=?", vals)


def list_dev_jobs_by_status(conn, status):
    """指定statusのジョブ一覧（Phase 4の中断ジョブ復旧などに使用）。"""
    rows = conn.execute(
        f"SELECT {','.join(_DEV_JOB_COLS)} FROM dev_jobs WHERE status=? "
        "ORDER BY id", (status,)).fetchall()
    return [_dev_job_row(r) for r in rows]


def latest_dev_job_for_cap(conn, cap_req_id):
    """起票に対する最新ジョブ（続きから再開の判定に使用）。無ければ None。"""
    return _dev_job_row(conn.execute(
        f"SELECT {','.join(_DEV_JOB_COLS)} FROM dev_jobs WHERE cap_req_id=? "
        "ORDER BY id DESC LIMIT 1", (cap_req_id,)).fetchone())


def claim_dev_job(conn, job_id, *, from_status, to_status, updated_at):
    """statusのcompare-and-set。二重👍や👍👎同時押しの競合で負けたら False。"""
    cur = conn.execute(
        "UPDATE dev_jobs SET status=?, updated_at=? WHERE id=? AND status=?",
        (to_status, updated_at, job_id, from_status))
    return cur.rowcount == 1


def supersede_built_jobs(conn, cap_req_id, *, updated_at):
    """同一起票の承認待ち(built)を superseded に落とす（新ジョブ開始時に呼ぶ）。
    worktree/ブランチ名は起票idで共有のため、作り直し後に古いサマリーへ👍されると
    「承認していない別の成果物」がmergeされてしまう事故を防ぐ。"""
    conn.execute(
        "UPDATE dev_jobs SET status='superseded', updated_at=? "
        "WHERE cap_req_id=? AND status='built'", (updated_at, cap_req_id))


def add_dev_lesson(conn, *, cap_req_id, job_id, kind, text, created_at):
    """開発の教訓を記録（kind: failed=失敗理由 / rejected=👎の理由 / note）。"""
    cur = conn.execute(
        """INSERT INTO dev_lessons(cap_req_id, job_id, kind, text, created_at)
           VALUES(?,?,?,?,?)""",
        (cap_req_id, job_id, kind, text, created_at))
    return cur.lastrowid


def recent_dev_lessons(conn, limit=5):
    """新しい順の教訓（改修プロンプトへの注入用）。"""
    rows = conn.execute(
        "SELECT kind, text FROM dev_lessons ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    return [{"kind": r[0], "text": r[1]} for r in rows]


# ---------------------------------------------------------------- agents (Phase 2)

def add_agent(conn, *, id, kind, name, avatar_url, home_channel_id,
              persona_file, skills_json, allowed_tools_json, created_at,
              home_channel_created=False):
    """Webhook人格を台帳に登録（既存idは設定を上書き＝再登録で更新）。"""
    conn.execute(
        """INSERT INTO agents(id, kind, name, avatar_url, home_channel_id,
               persona_file, skills_json, allowed_tools_json, status,
               created_at, home_channel_created)
           VALUES(?,?,?,?,?,?,?,?,'active',?,?)
           ON CONFLICT(id) DO UPDATE SET
               kind=excluded.kind, name=excluded.name,
               avatar_url=excluded.avatar_url,
               home_channel_id=excluded.home_channel_id,
               persona_file=excluded.persona_file,
               skills_json=excluded.skills_json,
               allowed_tools_json=excluded.allowed_tools_json,
               home_channel_created=excluded.home_channel_created,
               status='active'""",
        (id, kind, name, avatar_url, home_channel_id, persona_file,
         skills_json, allowed_tools_json, created_at,
         1 if home_channel_created else 0),
    )


def _agent_row(r):
    return {"id": r[0], "kind": r[1], "name": r[2], "avatar_url": r[3],
            "home_channel_id": r[4], "persona_file": r[5],
            "skills_json": r[6], "allowed_tools_json": r[7],
            "status": r[8], "created_at": r[9],
            "home_channel_created": bool(r[10])}


_AGENT_COLS = ("id, kind, name, avatar_url, home_channel_id, persona_file, "
               "skills_json, allowed_tools_json, status, created_at, "
               "home_channel_created")


def get_active_agents(conn):
    """active な台帳エージェント（Webhook人格）の一覧。"""
    rows = conn.execute(
        f"SELECT {_AGENT_COLS} FROM agents WHERE status='active' "
        "ORDER BY created_at ASC"
    ).fetchall()
    return [_agent_row(r) for r in rows]


def get_agent(conn, agent_id):
    """台帳エージェント1件（無ければ None）。"""
    row = conn.execute(
        f"SELECT {_AGENT_COLS} FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    return _agent_row(row) if row else None


def retire_agent(conn, agent_id):
    """台帳エージェントを退役（status=retired）。対象があれば True。"""
    cur = conn.execute(
        "UPDATE agents SET status='retired' WHERE id=? AND status='active'",
        (agent_id,))
    return cur.rowcount > 0


def add_pending_hire(conn, *, message_id, new_id, name, role, channel_name,
                     channel_id, proposed_by, created_at):
    """採用提案を承認待ちとして保存。採番idを返す。"""
    cur = conn.execute(
        """INSERT INTO pending_hires(message_id, new_id, name, role,
               channel_name, channel_id, status, proposed_by, created_at)
           VALUES(?,?,?,?,?,?,'pending',?,?)""",
        (message_id, new_id, name, role, channel_name, channel_id,
         proposed_by, created_at),
    )
    return cur.lastrowid


def get_pending_hire_by_message(conn, message_id):
    """提案メッセージIDから承認待ちの採用を引く（pendingのみ）。"""
    row = conn.execute(
        """SELECT id, message_id, new_id, name, role, channel_name,
                  channel_id, status, proposed_by
           FROM pending_hires WHERE message_id=? AND status='pending'""",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "message_id": row[1], "new_id": row[2],
            "name": row[3], "role": row[4], "channel_name": row[5],
            "channel_id": row[6], "status": row[7], "proposed_by": row[8]}


def set_hire_status(conn, hire_id, status):
    """採用提案のステータスを更新（done/rejected 等）。"""
    conn.execute("UPDATE pending_hires SET status=? WHERE id=?",
                 (status, hire_id))


def claim_pending_hire(conn, hire_id):
    """pending の採用を processing にアトミックに確保する。確保できたら True。
    SQLiteが UPDATE...WHERE status='pending' を直列化するので、二重承認でも
    勝った1タスクだけが True になり二重spawnを防ぐ。"""
    cur = conn.execute(
        "UPDATE pending_hires SET status='processing' "
        "WHERE id=? AND status='pending'", (hire_id,))
    return cur.rowcount == 1


def add_pending_fire(conn, *, message_id, target_id, target_name,
                     proposed_by, created_at):
    """解雇提案を承認待ちとして保存。採番idを返す。"""
    cur = conn.execute(
        """INSERT INTO pending_fires(message_id, target_id, target_name,
               status, proposed_by, created_at)
           VALUES(?,?,?,'pending',?,?)""",
        (message_id, target_id, target_name, proposed_by, created_at),
    )
    return cur.lastrowid


def get_pending_fire_by_message(conn, message_id):
    """提案メッセージIDから承認待ちの解雇を引く（pendingのみ）。"""
    row = conn.execute(
        """SELECT id, target_id, target_name, proposed_by
           FROM pending_fires WHERE message_id=? AND status='pending'""",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "target_id": row[1], "target_name": row[2],
            "proposed_by": row[3]}


def claim_pending_fire(conn, fire_id):
    """pending の解雇を processing にアトミック確保（二重実行防止）。"""
    cur = conn.execute(
        "UPDATE pending_fires SET status='processing' "
        "WHERE id=? AND status='pending'", (fire_id,))
    return cur.rowcount == 1


def set_fire_status(conn, fire_id, status):
    conn.execute("UPDATE pending_fires SET status=? WHERE id=?",
                 (status, fire_id))


def message_author(conn, message_id):
    """メッセージの著者ID（無ければ None）。リアクション対象の照会用。"""
    row = conn.execute(
        "SELECT author_id FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    return row[0] if row else None


def get_message(conn, message_id):
    """メッセージ1件を著者名・ch名つきで返す（無ければ None）。
    メッセージリンク/ID参照（msgref）の解決用。deleted も含めて返す
    （明示的に指定された投稿は削除済みでも参照できるようにする）。"""
    row = conn.execute(
        """SELECT m.id, m.channel_id, c.name, u.display_name, m.author_id,
                  m.content, m.created_at, m.deleted
           FROM messages m
           LEFT JOIN channels c ON c.id=m.channel_id
           LEFT JOIN users u ON u.id=m.author_id
           WHERE m.id=?""",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return {"message_id": row[0], "channel_id": row[1], "channel": row[2],
            "author": row[3], "author_id": row[4], "content": row[5],
            "created_at": row[6], "deleted": bool(row[7])}


def add_feedback(conn, *, message_id, agent_id, kind, value, user_id,
                 created_at):
    """👍👎等のフィードバックを記録（同一(msg,user,value)は重複無視）。"""
    conn.execute(
        """INSERT INTO feedback(message_id, agent_id, kind, value, user_id,
                                created_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(message_id, user_id, value) DO NOTHING""",
        (message_id, agent_id, kind, value, user_id, created_at),
    )


def remove_feedback(conn, *, message_id, user_id, value):
    """リアクション解除時にフィードバックを取り消す。"""
    conn.execute(
        "DELETE FROM feedback WHERE message_id=? AND user_id=? AND value=?",
        (message_id, user_id, value),
    )


# ---------------------------------------------------------- proactive (v3 Phase A)

def get_proactive_state(conn, agent_id):
    """観察ループのチェックポイント（無ければ None＝初回）。"""
    row = conn.execute(
        """SELECT last_checked_message_id, last_run_at
           FROM proactive_state WHERE agent_id=?""", (agent_id,)).fetchone()
    if row is None:
        return None
    return {"last_checked_message_id": row[0], "last_run_at": row[1]}


def set_proactive_state(conn, agent_id, *, last_checked_message_id,
                        last_run_at):
    conn.execute(
        """INSERT INTO proactive_state(agent_id, last_checked_message_id,
                                       last_run_at)
           VALUES(?,?,?)
           ON CONFLICT(agent_id) DO UPDATE SET
               last_checked_message_id=excluded.last_checked_message_id,
               last_run_at=excluded.last_run_at""",
        (agent_id, last_checked_message_id, last_run_at))


def max_message_id(conn):
    """保存済みの最大message_id（無ければ0）。checkpoint初期化用。"""
    row = conn.execute("SELECT MAX(id) FROM messages").fetchone()
    return row[0] if row and row[0] else 0


def human_messages_after(conn, after_id, exclude_channel_ids=(), limit=80):
    """全chの after_id より新しい「人間の」発言を古い順で返す（観察ループ用）。
    Bot・Webhook（usersに行が無い投稿者はBot扱い）・削除済み・空本文は除く。"""
    excl = [int(c) for c in (exclude_channel_ids or ())]
    sql = """SELECT m.id, m.channel_id, c.name, m.author_id, u.display_name,
                    m.content, m.created_at, m.reply_to
             FROM messages m
             LEFT JOIN users u ON u.id=m.author_id
             LEFT JOIN channels c ON c.id=m.channel_id
             WHERE m.id > ? AND m.deleted=0
               AND COALESCE(u.is_bot, 1)=0
               AND m.content IS NOT NULL AND m.content != ''"""
    params = [after_id]
    if excl:
        sql += f" AND m.channel_id NOT IN ({','.join('?' * len(excl))})"
        params += excl
    sql += " ORDER BY m.id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{"id": r[0], "channel_id": r[1], "channel": r[2] or "?",
             "author_id": r[3], "author": r[4] or "?", "content": r[5] or "",
             "created_at": r[6], "reply_to": r[7]} for r in rows]


def authors_of(conn, message_ids):
    """メッセージid→投稿者idの対応（宛先つき発言の除外用）。"""
    ids = [int(i) for i in message_ids if i]
    if not ids:
        return {}
    rows = conn.execute(
        f"""SELECT id, author_id FROM messages
            WHERE id IN ({','.join('?' * len(ids))})""", ids).fetchall()
    return {r[0]: r[1] for r in rows}


def claim_trigger(conn, message_id, agent_id, kind, created_at):
    """トリガー発言のエージェント横断クレーム（先勝ちCAS）。
    既に他の誰か（自分含む）がクレーム済みなら False。"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO proactive_claims(trigger_message_id,
               agent_id, kind, created_at) VALUES(?,?,?,?)""",
        (message_id, agent_id, kind, created_at))
    return cur.rowcount == 1


def agent_posted_after(conn, channel_id, author_id, after_message_id):
    """対象者がそのchで指定メッセージ以降に発言済みか（引き継ぎ前チェック）。"""
    return conn.execute(
        """SELECT 1 FROM messages WHERE channel_id=? AND author_id=?
           AND id > ? AND deleted=0 LIMIT 1""",
        (channel_id, author_id, after_message_id)).fetchone() is not None


def add_proactive_log(conn, *, agent_id, kind, action, channel_id=None,
                      trigger_message_id=None, posted_message_id=None,
                      detail=None, created_at=None):
    """自発発言/沈黙の記録。採番idを返す。"""
    cur = conn.execute(
        """INSERT INTO proactive_log(agent_id, kind, action, channel_id,
               trigger_message_id, posted_message_id, detail, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (agent_id, kind, action, channel_id, trigger_message_id,
         posted_message_id, detail, created_at))
    return cur.lastrowid


def count_proactive_spoken_since(conn, agent_id, since):
    """since（fmt形式文字列）以降の自発「発言」数。日次枠の執行に使う。
    納期声かけ(action='nudge')・追跡開始(action='track')は枠と別勘定で数えない。"""
    row = conn.execute(
        """SELECT COUNT(*) FROM proactive_log
           WHERE agent_id=? AND action='spoke' AND created_at >= ?""",
        (agent_id, since)).fetchone()
    return row[0]


def get_proactive_quota(conn, agent_id, default):
    """日次枠。proactive_settings の上書きがあればそれ、無ければ default。"""
    row = conn.execute(
        "SELECT daily_quota FROM proactive_settings WHERE agent_id=?",
        (agent_id,)).fetchone()
    return row[0] if row and row[0] is not None else default


def set_proactive_quota(conn, agent_id, daily_quota, updated_at):
    conn.execute(
        """INSERT INTO proactive_settings(agent_id, daily_quota, updated_at)
           VALUES(?,?,?)
           ON CONFLICT(agent_id) DO UPDATE SET
               daily_quota=excluded.daily_quota,
               updated_at=excluded.updated_at""",
        (agent_id, daily_quota, updated_at))


def proactive_stats_since(conn, since):
    """週次レポート用の集計。agent_id -> {spoke_by_kind, silent, nudge, track,
    up, down} と 追跡中タスク数を返す（決定論・コードで集計する）。"""
    stats = {}

    def _slot(aid):
        return stats.setdefault(aid, {"spoke_by_kind": {}, "silent": 0,
                                      "nudge": 0, "track": 0,
                                      "up": 0, "down": 0})

    for aid, action, kind, n in conn.execute(
            """SELECT agent_id, action, kind, COUNT(*) FROM proactive_log
               WHERE created_at >= ? GROUP BY agent_id, action, kind""",
            (since,)).fetchall():
        s = _slot(aid)
        if action == "spoke":
            s["spoke_by_kind"][kind] = s["spoke_by_kind"].get(kind, 0) + n
        elif action in ("silent", "nudge", "track"):
            s[action] += n
    for aid, value, n in conn.execute(
            """SELECT p.agent_id, f.value, COUNT(*)
               FROM proactive_log p
               JOIN feedback f ON f.message_id = p.posted_message_id
               WHERE p.created_at >= ? AND p.action='spoke'
               GROUP BY p.agent_id, f.value""", (since,)).fetchall():
        if value in ("up", "down"):
            _slot(aid)[value] += n
    open_items = conn.execute(
        "SELECT COUNT(*) FROM action_items WHERE status='open'").fetchone()[0]
    return {"agents": stats, "open_action_items": open_items}


# -------------------------------------------------------- action_items (v3 Phase B)

_ACTION_COLS = ("id", "agent_id", "source_message_id", "channel_id",
                "confirm_message_id", "task", "owners", "due_date", "urgent",
                "status", "nudge_stage", "last_nudge_message_id", "created_at")


def _action_row(row):
    return dict(zip(_ACTION_COLS, row)) if row else None


def add_action_item(conn, *, agent_id, source_message_id, channel_id, task,
                    owners, due_date, urgent, created_at):
    """議事録から抽出したTODOを1件保存。採番idを返す。"""
    cur = conn.execute(
        """INSERT INTO action_items(agent_id, source_message_id, channel_id,
               task, owners, due_date, urgent, status, nudge_stage, created_at)
           VALUES(?,?,?,?,?,?,?,'open','none',?)""",
        (agent_id, source_message_id, channel_id, task, owners, due_date,
         1 if urgent else 0, created_at))
    return cur.lastrowid


def set_action_confirm_message(conn, item_ids, message_id):
    """追跡宣言メッセージのidを紐付ける（❌での一括取り消し用）。"""
    if not item_ids:
        return
    ph = ",".join("?" * len(item_ids))
    conn.execute(
        f"UPDATE action_items SET confirm_message_id=? WHERE id IN ({ph})",
        [message_id, *item_ids])


def open_action_items(conn, agent_id):
    """追跡中（open）のタスクを期日順で返す。"""
    rows = conn.execute(
        f"""SELECT {','.join(_ACTION_COLS)} FROM action_items
            WHERE agent_id=? AND status='open' ORDER BY due_date ASC""",
        (agent_id,)).fetchall()
    return [_action_row(r) for r in rows]


def update_action_nudge(conn, item_id, *, stage, message_id):
    """声かけ済み段階を前進させる（重複声かけ防止）。"""
    conn.execute(
        """UPDATE action_items SET nudge_stage=?, last_nudge_message_id=?
           WHERE id=?""", (stage, message_id, item_id))


def complete_action_by_nudge_message(conn, message_id):
    """声かけメッセージへの✅でタスクを完了にする。対象があればそのdictを返す。"""
    row = conn.execute(
        f"""SELECT {','.join(_ACTION_COLS)} FROM action_items
            WHERE last_nudge_message_id=? AND status='open'""",
        (message_id,)).fetchone()
    if row is None:
        return None
    item = _action_row(row)
    conn.execute("UPDATE action_items SET status='done' WHERE id=?",
                 (item["id"],))
    return item


def drop_actions_by_confirm_message(conn, message_id):
    """追跡宣言メッセージへの❌で、その議事録のタスク追跡を一括取り消す。
    取り消した件数を返す。"""
    cur = conn.execute(
        """UPDATE action_items SET status='dropped'
           WHERE confirm_message_id=? AND status='open'""", (message_id,))
    return cur.rowcount


def get_action_item(conn, item_id, agent_id):
    """id指定で1件取得（他エージェントの追跡は見せない）。無ければ None。"""
    row = conn.execute(
        f"""SELECT {','.join(_ACTION_COLS)} FROM action_items
            WHERE id=? AND agent_id=?""", (item_id, agent_id)).fetchone()
    return _action_row(row)


def close_action_item(conn, item_id, agent_id, *, status):
    """会話マーカーからの追跡終了（status: 'cancelled'/'done'）。
    open のものだけ閉じ、閉じられたら True。"""
    cur = conn.execute(
        """UPDATE action_items SET status=?
           WHERE id=? AND agent_id=? AND status='open'""",
        (status, item_id, agent_id))
    return cur.rowcount > 0


# -------------------------------------------------------- homework (v3 Phase E)

_HOMEWORK_COLS = ("id", "agent_id", "source_message_id", "channel_id", "owner",
                  "task", "committed_date", "follow_up_date", "status",
                  "followup_message_id", "created_at")


def _homework_row(row):
    return dict(zip(_HOMEWORK_COLS, row)) if row else None


def add_homework_item(conn, *, agent_id, source_message_id, channel_id, owner,
                      task, committed_date, follow_up_date, created_at):
    """検知した自己コミットを1件保存。採番idを返す（同一(agent,source)は
    重複無視で None＝二重追跡しない）。"""
    cur = conn.execute(
        """INSERT INTO homework_items(agent_id, source_message_id, channel_id,
               owner, task, committed_date, follow_up_date, status, created_at)
           VALUES(?,?,?,?,?,?,?,'open',?)
           ON CONFLICT(agent_id, source_message_id) DO NOTHING""",
        (agent_id, source_message_id, channel_id, owner, task, committed_date,
         follow_up_date, created_at))
    return cur.lastrowid if cur.rowcount else None


def open_homework_due(conn, agent_id, on_or_before):
    """follow_up_date が on_or_before 以下の open な宿題（期日の古い順）。"""
    rows = conn.execute(
        f"""SELECT {','.join(_HOMEWORK_COLS)} FROM homework_items
            WHERE agent_id=? AND status='open' AND follow_up_date<=?
            ORDER BY follow_up_date ASC""",
        (agent_id, on_or_before)).fetchall()
    return [_homework_row(r) for r in rows]


def set_homework_status(conn, item_id, status, followup_message_id=None):
    """宿題の状態を更新（声かけ済み='asked' / 期限切れ='expired' 等）。"""
    conn.execute(
        "UPDATE homework_items SET status=?, followup_message_id=? WHERE id=?",
        (status, followup_message_id, item_id))


# -------------------------------------------------------- roadmap（進化バックログ）

_ROADMAP_COLS = ("id", "title", "description", "category", "tier", "route",
                 "effect", "cost", "status", "card_message_id", "cap_req_id",
                 "created_at", "decided_at")


def _roadmap_row(row):
    return dict(zip(_ROADMAP_COLS, row)) if row else None


def roadmap_seed_item(conn, *, id, title, description, category, tier, route,
                      effect, cost, created_at):
    """シード投入（既存idは触らない＝status等の進行状態を再seedで壊さない）。
    新規に入れたら True。"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO roadmap_items(id, title, description,
               category, tier, route, effect, cost, status, created_at)
           VALUES(?,?,?,?,?,?,?,?,'pending',?)""",
        (id, title, description, category, tier, route, effect, cost,
         created_at))
    return cur.rowcount == 1


def roadmap_next_pending(conn):
    """次に提案すべき1件（費用対効果スコア= effect*2-cost の降順・同点はid順）。"""
    return _roadmap_row(conn.execute(
        f"""SELECT {','.join(_ROADMAP_COLS)} FROM roadmap_items
            WHERE status='pending'
            ORDER BY (effect*2 - cost) DESC, id ASC LIMIT 1""").fetchone())


def roadmap_proposed(conn):
    """提案中（承認待ちカード）の1件。無ければ None。"""
    return _roadmap_row(conn.execute(
        f"""SELECT {','.join(_ROADMAP_COLS)} FROM roadmap_items
            WHERE status='proposed' ORDER BY decided_at ASC LIMIT 1"""
    ).fetchone())


def roadmap_mark_proposed(conn, item_id, message_id, at):
    """カード投稿済みにする（pending→proposed のCAS。負けたら False）。"""
    cur = conn.execute(
        """UPDATE roadmap_items SET status='proposed', card_message_id=?,
               decided_at=? WHERE id=? AND status='pending'""",
        (message_id, at, item_id))
    return cur.rowcount == 1


def roadmap_by_message(conn, message_id):
    """カードメッセージidから提案中の項目を引く（👍👎の対象解決）。"""
    return _roadmap_row(conn.execute(
        f"""SELECT {','.join(_ROADMAP_COLS)} FROM roadmap_items
            WHERE card_message_id=? AND status='proposed'""",
        (message_id,)).fetchone())


def roadmap_claim_decision(conn, item_id, to_status, at):
    """proposed→決定のCAS（👍👎同時押しは片方だけが勝つ）。"""
    cur = conn.execute(
        """UPDATE roadmap_items SET status=?, decided_at=?
           WHERE id=? AND status='proposed'""", (to_status, at, item_id))
    return cur.rowcount == 1


def roadmap_set_cap(conn, item_id, cap_req_id):
    conn.execute("UPDATE roadmap_items SET cap_req_id=? WHERE id=?",
                 (cap_req_id, item_id))


def roadmap_counts(conn):
    """status→件数（!roadmap の進捗表示用）。"""
    return dict(conn.execute(
        "SELECT status, COUNT(*) FROM roadmap_items GROUP BY status"
    ).fetchall())


def roadmap_session_queue(conn, limit=10):
    """管理者セッション行きの承認済みリスト（次のセッションで拾う）。"""
    rows = conn.execute(
        f"""SELECT {','.join(_ROADMAP_COLS)} FROM roadmap_items
            WHERE status='queued_session' ORDER BY decided_at ASC LIMIT ?""",
        (limit,)).fetchall()
    return [_roadmap_row(r) for r in rows]


# ----------------------------------------------------- deploy_history（revert/canary）

_DEPLOY_COLS = ("job_id", "cap_req_id", "pre_sha", "post_sha", "files",
                "deployed_at", "reverted_at", "canary_baseline",
                "canary_status")


def _deploy_row(row):
    return dict(zip(_DEPLOY_COLS, row)) if row else None


def add_deploy_record(conn, *, job_id, cap_req_id, pre_sha, post_sha, files,
                      deployed_at, canary_baseline):
    conn.execute(
        """INSERT OR REPLACE INTO deploy_history(job_id, cap_req_id, pre_sha,
               post_sha, files, deployed_at, canary_baseline, canary_status)
           VALUES(?,?,?,?,?,?,?,'watching')""",
        (job_id, cap_req_id, pre_sha, post_sha, files, deployed_at,
         canary_baseline))


def latest_deploy_for_cap(conn, cap_req_id):
    """起票idの最新の未revertデプロイ（!revert の対象）。"""
    return _deploy_row(conn.execute(
        f"""SELECT {','.join(_DEPLOY_COLS)} FROM deploy_history
            WHERE cap_req_id=? AND reverted_at IS NULL
            ORDER BY job_id DESC LIMIT 1""", (cap_req_id,)).fetchone())


def mark_deploy_reverted(conn, job_id, at):
    conn.execute(
        """UPDATE deploy_history SET reverted_at=?, canary_status='ok'
           WHERE job_id=?""", (at, job_id))


def watching_canaries(conn):
    """カナリア監視中のデプロイ一覧。"""
    rows = conn.execute(
        f"""SELECT {','.join(_DEPLOY_COLS)} FROM deploy_history
            WHERE canary_status='watching' AND reverted_at IS NULL"""
    ).fetchall()
    return [_deploy_row(r) for r in rows]


def set_canary_status(conn, job_id, status):
    conn.execute("UPDATE deploy_history SET canary_status=? WHERE job_id=?",
                 (status, job_id))


# ------------------------------------------- cap_proposals（起票の自動拾い上げ / RM#21）

def max_capability_request_id(conn):
    """現在の最大起票id（checkpoint初期化用。過去分を蒸し返さない）。"""
    row = conn.execute("SELECT MAX(id) FROM capability_requests").fetchone()
    return row[0] if row and row[0] else 0


def next_unproposed_capability(conn, after_id):
    """checkpointより新しい open のオーガニック起票を1件（古い順）。
    ロードマップ由来（agent_id='roadmap'）と提案済みは除く。"""
    row = conn.execute(
        """SELECT id, agent_id, description, requested_by
           FROM capability_requests
           WHERE id > ? AND status='open' AND agent_id != 'roadmap'
             AND id NOT IN (SELECT cap_req_id FROM cap_proposals)
           ORDER BY id ASC LIMIT 1""", (after_id,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "agent_id": row[1], "description": row[2],
            "requested_by": row[3]}


def add_cap_proposal(conn, *, cap_req_id, message_id, created_at):
    conn.execute(
        """INSERT OR REPLACE INTO cap_proposals(cap_req_id, message_id,
               status, created_at) VALUES(?,?,'proposed',?)""",
        (cap_req_id, message_id, created_at))


def cap_proposal_pending(conn):
    """提案中（👍👎待ち）の起票提案があるか（1件ずつ運用）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM cap_proposals WHERE status='proposed'"
    ).fetchone()[0] > 0


def cap_proposal_by_message(conn, message_id):
    """提案メッセージidから対象の起票idを引く（無ければ None）。"""
    row = conn.execute(
        """SELECT cap_req_id FROM cap_proposals
           WHERE message_id=? AND status='proposed'""",
        (message_id,)).fetchone()
    return row[0] if row else None


def claim_cap_proposal(conn, cap_req_id, to_status, at):
    """proposed→決定のCAS（👍👎同時押しは片方だけが勝つ）。"""
    cur = conn.execute(
        """UPDATE cap_proposals SET status=?, decided_at=?
           WHERE cap_req_id=? AND status='proposed'""",
        (to_status, at, cap_req_id))
    return cur.rowcount == 1


# ----------------------------------------------------- decisions（決定事項台帳 / RM#4）

def add_decision(conn, *, agent_id, decision, topic, source_kind,
                 source_message_id, channel_id, decided_on, created_at):
    """決定事項を1件記録。採番idを返す。"""
    cur = conn.execute(
        """INSERT INTO decisions(agent_id, decision, topic, source_kind,
               source_message_id, channel_id, decided_on, status, created_at)
           VALUES(?,?,?,?,?,?,?,'active',?)""",
        (agent_id, decision, topic, source_kind, source_message_id,
         channel_id, decided_on, created_at))
    return cur.lastrowid


def search_decisions(conn, keywords, limit=8):
    """決定事項をキーワードLIKEで検索（activeのみ・新しい順）。
    キーワードが空/短すぎなら直近limit件を返す。"""
    base = ("SELECT id, decision, topic, source_message_id, channel_id, "
            "decided_on FROM decisions WHERE status='active'")
    kws = [k.strip() for k in (keywords or []) if len(k.strip()) >= 2][:8]
    if kws:
        clauses = " OR ".join("(decision LIKE ? OR topic LIKE ?)"
                              for _ in kws)
        params = []
        for k in kws:
            params += [f"%{k}%", f"%{k}%"]
        rows = conn.execute(
            base + f" AND ({clauses}) ORDER BY id DESC LIMIT ?",
            params + [limit]).fetchall()
    else:
        rows = conn.execute(base + " ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{"id": r[0], "decision": r[1], "topic": r[2],
             "source_message_id": r[3], "channel_id": r[4],
             "decided_on": r[5]} for r in rows]


def count_decisions(conn):
    """activeな決定事項の総数（週次レポート用）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE status='active'").fetchone()[0]


# --------------------------------------------------- glossary（単語帳 / RM#5）

def glossary_pairs(conn):
    """(誤, 正) ペア一覧（登録順）。"""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT wrong, correct FROM glossary ORDER BY created_at ASC"
    ).fetchall()]


def add_glossary_term(conn, *, wrong, correct, created_by, created_at):
    conn.execute(
        """INSERT INTO glossary(wrong, correct, created_by, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(wrong) DO UPDATE SET correct=excluded.correct,
               created_by=excluded.created_by,
               created_at=excluded.created_at""",
        (wrong, correct, created_by, created_at))


def remove_glossary_term(conn, wrong):
    """単語帳から削除（あれば True）。"""
    cur = conn.execute("DELETE FROM glossary WHERE wrong=?", (wrong,))
    return cur.rowcount > 0


def terms_all(conn):
    """固有名詞辞書の全エントリ（登録順）。"""
    return [{"term": r[0], "description": r[1] or ""} for r in conn.execute(
        "SELECT term, description FROM terms ORDER BY created_at ASC"
    ).fetchall()]


def add_term(conn, *, term, description, created_by, created_at):
    conn.execute(
        """INSERT INTO terms(term, description, created_by, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(term) DO UPDATE SET description=excluded.description,
               created_by=excluded.created_by,
               created_at=excluded.created_at""",
        (term, description, created_by, created_at))


def remove_term(conn, term):
    cur = conn.execute("DELETE FROM terms WHERE term=?", (term,))
    return cur.rowcount > 0


def glossary_fix_existing(conn, wrong, correct):
    """伝染済みの派生データ（決定台帳・納期タスク）を遡及修正する。
    元発言（messagesアーカイブ）は史実なので書き換えない。修正行数を返す。"""
    n = 0
    n += conn.execute(
        """UPDATE decisions SET decision=REPLACE(decision, ?, ?),
               topic=REPLACE(topic, ?, ?)
           WHERE decision LIKE '%' || ? || '%' OR topic LIKE '%' || ? || '%'""",
        (wrong, correct, wrong, correct, wrong, wrong)).rowcount
    n += conn.execute(
        """UPDATE action_items SET task=REPLACE(task, ?, ?)
           WHERE task LIKE '%' || ? || '%'""",
        (wrong, correct, wrong)).rowcount
    n += conn.execute(
        """UPDATE profiles SET profile=REPLACE(profile, ?, ?)
           WHERE profile LIKE '%' || ? || '%'""",
        (wrong, correct, wrong)).rowcount
    return n


# --------------------------------------------------- profiles（人物プロファイル / RM#1）

def get_profile(conn, user_id):
    """メンバーのプロファイル（無ければ None）。"""
    row = conn.execute(
        """SELECT user_id, display_name, profile, covered_until_message_id,
                  updated_at FROM profiles WHERE user_id=?""",
        (user_id,)).fetchone()
    if row is None:
        return None
    return {"user_id": row[0], "display_name": row[1], "profile": row[2],
            "covered_until": row[3], "updated_at": row[4]}


def upsert_profile(conn, *, user_id, display_name, profile, covered_until,
                   updated_at):
    conn.execute(
        """INSERT INTO profiles(user_id, display_name, profile,
               covered_until_message_id, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               display_name=excluded.display_name,
               profile=excluded.profile,
               covered_until_message_id=excluded.covered_until_message_id,
               updated_at=excluded.updated_at""",
        (user_id, display_name, profile, covered_until, updated_at))


def profile_update_candidate(conn, min_new=20):
    """未反映の発言が最も溜まっている人間メンバー1人（居なければ None）。"""
    row = conn.execute(
        """SELECT m.author_id, u.display_name, COUNT(*) AS n
           FROM messages m
           JOIN users u ON u.id = m.author_id
           LEFT JOIN profiles p ON p.user_id = m.author_id
           WHERE m.deleted=0 AND u.is_bot=0
             AND m.content IS NOT NULL AND m.content != ''
             AND m.id > COALESCE(p.covered_until_message_id, 0)
           GROUP BY m.author_id
           HAVING n >= ?
           ORDER BY n DESC LIMIT 1""", (min_new,)).fetchone()
    if row is None:
        return None
    return {"user_id": row[0], "display_name": row[1] or "?", "new": row[2]}


def user_messages_after(conn, user_id, after_id, limit=100):
    """特定メンバーの発言を古い順で（プロファイル蒸留の材料）。"""
    rows = conn.execute(
        """SELECT m.id, c.name, m.content, m.created_at
           FROM messages m LEFT JOIN channels c ON c.id=m.channel_id
           WHERE m.author_id=? AND m.deleted=0 AND m.id > ?
             AND m.content IS NOT NULL AND m.content != ''
           ORDER BY m.id ASC LIMIT ?""",
        (user_id, after_id, limit)).fetchall()
    return [{"id": r[0], "channel": r[1] or "?", "content": r[2],
             "created_at": r[3]} for r in rows]


# --------------------------------------------------- events（イベント逆算 / RM#35）

_EVENT_COLS = ("id", "agent_id", "name", "event_date", "source_decision_id",
               "channel_id", "milestones_json", "proposal_message_id",
               "status", "created_at")


def _event_row(row):
    return dict(zip(_EVENT_COLS, row)) if row else None


def add_event(conn, *, agent_id, name, event_date, source_decision_id,
              channel_id, milestones_json, status, created_at):
    """イベントを登録（同じ決定からは1回だけ＝重複提案防止）。採番id。"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO events(agent_id, name, event_date,
               source_decision_id, channel_id, milestones_json, status,
               created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (agent_id, name, event_date, source_decision_id, channel_id,
         milestones_json, status, created_at))
    return cur.lastrowid if cur.rowcount else None


def undetected_decisions_for_events(conn, limit=50):
    """イベント検知にかける最近の決定（既にeventsに紐付いた分は除く）。"""
    rows = conn.execute(
        """SELECT id, decision, channel_id, source_message_id, decided_on
           FROM decisions
           WHERE status='active'
             AND id NOT IN (SELECT source_decision_id FROM events
                            WHERE source_decision_id IS NOT NULL)
           ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    return [{"id": r[0], "decision": r[1], "channel_id": r[2],
             "source_message_id": r[3], "decided_on": r[4]} for r in rows]


def set_event_proposal(conn, event_id, message_id):
    conn.execute(
        "UPDATE events SET proposal_message_id=? WHERE id=?",
        (message_id, event_id))


def event_by_proposal(conn, message_id):
    """✅❌の対象イベント（proposed のみ）。"""
    return _event_row(conn.execute(
        f"""SELECT {','.join(_EVENT_COLS)} FROM events
            WHERE proposal_message_id=? AND status='proposed'""",
        (message_id,)).fetchone())


def events_planned_after(conn, after_id, limit=5):
    """✅承認済み(planned)イベントのうち id > after のもの（RM#40の検知用）。"""
    rows = conn.execute(
        f"""SELECT {','.join(_EVENT_COLS)} FROM events
            WHERE status='planned' AND id > ? ORDER BY id ASC LIMIT ?""",
        (after_id, limit)).fetchall()
    return [_event_row(r) for r in rows]


def claim_event(conn, event_id, to_status):
    """proposed→決定のCAS（✅❌の排他）。"""
    cur = conn.execute(
        "UPDATE events SET status=? WHERE id=? AND status='proposed'",
        (to_status, event_id))
    return cur.rowcount == 1


# --------------------------------------------------- rescues（未回答質問の救済 / RM#31）

def unanswered_question_candidates(conn, *, since, until,
                                   exclude_channel_ids=(), limit=3):
    """疑問符つきの人間発言のうち、リプライが1件も無く・未判定のものを古い順で。
    since/until は created_at と同形式（UTC ISO）の文字列比較。"""
    excl = [int(c) for c in (exclude_channel_ids or ())]
    sql = """SELECT m.id, m.channel_id, c.name, m.author_id, u.display_name,
                    m.content, m.created_at
             FROM messages m
             LEFT JOIN users u ON u.id=m.author_id
             LEFT JOIN channels c ON c.id=m.channel_id
             WHERE m.deleted=0 AND COALESCE(u.is_bot, 1)=0
               AND m.created_at >= ? AND m.created_at <= ?
               AND (m.content LIKE '%？%' OR m.content LIKE '%?%')
               AND NOT EXISTS (SELECT 1 FROM messages r
                               WHERE r.reply_to = m.id AND r.deleted=0)
               AND m.id NOT IN (SELECT message_id FROM rescues)"""
    params = [since, until]
    if excl:
        sql += f" AND m.channel_id NOT IN ({','.join('?' * len(excl))})"
        params += excl
    sql += " ORDER BY m.id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{"id": r[0], "channel_id": r[1], "channel": r[2] or "?",
             "author_id": r[3], "author": r[4] or "?", "content": r[5] or "",
             "created_at": r[6]} for r in rows]


def add_rescue(conn, *, message_id, agent_id, status, posted_message_id,
               created_at):
    """救済判定の結果を記録（同じ質問を二度判定しないための台帳）。"""
    conn.execute(
        """INSERT OR REPLACE INTO rescues(message_id, agent_id, status,
               posted_message_id, created_at)
           VALUES(?,?,?,?,?)""",
        (message_id, agent_id, status, posted_message_id, created_at))


def add_golden(conn, *, agent_id, question, answer, source_answer_id,
               channel_id, created_at):
    """👍つきQ&Aを評価セットへ（同じ回答からは1回だけ）。新規なら True。"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO golden_set(agent_id, question, answer,
               source_answer_id, channel_id, created_at)
           VALUES(?,?,?,?,?,?)""",
        (agent_id, question, answer, source_answer_id, channel_id,
         created_at))
    return cur.rowcount == 1


def count_golden(conn):
    return conn.execute("SELECT COUNT(*) FROM golden_set").fetchone()[0]


def preceding_human_message(conn, channel_id, before_id):
    """回答の直前にある人間の発言（=質問とみなす）。無ければ None。"""
    row = conn.execute(
        """SELECT m.content FROM messages m
           JOIN users u ON u.id=m.author_id
           WHERE m.channel_id=? AND m.id < ? AND m.deleted=0 AND u.is_bot=0
             AND m.content IS NOT NULL AND m.content != ''
           ORDER BY m.id DESC LIMIT 1""",
        (channel_id, before_id)).fetchone()
    return row[0] if row else None


def selfreview_avg_since(conn, since):
    """投稿セルフレビュー（RM#14）の平均点と件数。detailは「点数|一言」形式。"""
    row = conn.execute(
        """SELECT AVG(CAST(substr(detail, 1, 1) AS INTEGER)), COUNT(*)
           FROM proactive_log
           WHERE kind='selfreview' AND action='score' AND created_at >= ?""",
        (since,)).fetchone()
    return {"avg": row[0], "n": row[1] or 0}


def silent_candidates_since(conn, agent_id, since, limit=10):
    """沈黙の正解率検証（RM#13）の対象: 候補になったが沈黙した記録。"""
    rows = conn.execute(
        """SELECT id, trigger_message_id, kind, channel_id
           FROM proactive_log
           WHERE agent_id=? AND action='silent'
             AND trigger_message_id IS NOT NULL AND kind != 'none'
             AND created_at >= ?
           ORDER BY id DESC LIMIT ?""", (agent_id, since, limit)).fetchall()
    return [{"log_id": r[0], "trigger_message_id": r[1], "kind": r[2],
             "channel_id": r[3]} for r in rows]


def rules_all_active(conn):
    """全エージェントのactiveルール（棚卸し用）。"""
    rows = conn.execute(
        """SELECT id, agent_id, scope, rule_text, expires_at FROM rules
           WHERE active=1 ORDER BY agent_id, id""").fetchall()
    return [{"id": r[0], "agent_id": r[1], "scope": r[2], "rule_text": r[3],
             "expires_at": r[4]} for r in rows]


def add_rule_review(conn, *, payload_json, created_at):
    cur = conn.execute(
        """INSERT INTO rule_reviews(payload_json, status, created_at)
           VALUES(?, 'proposed', ?)""", (payload_json, created_at))
    return cur.lastrowid


def set_rule_review_message(conn, review_id, message_id):
    conn.execute("UPDATE rule_reviews SET proposal_message_id=? WHERE id=?",
                 (message_id, review_id))


def rule_review_by_message(conn, message_id):
    row = conn.execute(
        """SELECT id, payload_json FROM rule_reviews
           WHERE proposal_message_id=? AND status='proposed'""",
        (message_id,)).fetchone()
    return {"id": row[0], "payload_json": row[1]} if row else None


def claim_rule_review(conn, review_id, to_status):
    cur = conn.execute(
        "UPDATE rule_reviews SET status=? WHERE id=? AND status='proposed'",
        (to_status, review_id))
    return cur.rowcount == 1


def add_auto_proposal(conn, *, payload_json, created_at):
    cur = conn.execute(
        """INSERT INTO auto_proposals(payload_json, status, created_at)
           VALUES(?, 'proposed', ?)""", (payload_json, created_at))
    return cur.lastrowid


def set_auto_proposal_message(conn, proposal_id, message_id):
    conn.execute(
        "UPDATE auto_proposals SET proposal_message_id=? WHERE id=?",
        (message_id, proposal_id))


def auto_proposal_by_message(conn, message_id):
    row = conn.execute(
        """SELECT id, payload_json FROM auto_proposals
           WHERE proposal_message_id=? AND status='proposed'""",
        (message_id,)).fetchone()
    return {"id": row[0], "payload_json": row[1]} if row else None


def claim_auto_proposal(conn, proposal_id, to_status):
    cur = conn.execute(
        "UPDATE auto_proposals SET status=? WHERE id=? AND status='proposed'",
        (to_status, proposal_id))
    return cur.rowcount == 1


def add_episode(conn, *, channel_id, happened_on, kind, summary, source_ref,
                created_at):
    """出来事を1件記録（RM#3）。"""
    cur = conn.execute(
        """INSERT INTO episodes(channel_id, happened_on, kind, summary,
               source_ref, created_at) VALUES(?,?,?,?,?,?)""",
        (channel_id, happened_on, kind, summary, source_ref, created_at))
    return cur.lastrowid


def episode_exists(conn, kind, source_ref):
    """同じ出来事を二重記録しないための存在チェック。"""
    return conn.execute(
        "SELECT 1 FROM episodes WHERE kind=? AND source_ref=? LIMIT 1",
        (kind, source_ref)).fetchone() is not None


def channel_episodes(conn, channel_id, limit=12):
    """チャンネルの出来事タイムライン（新しい順）。"""
    rows = conn.execute(
        """SELECT happened_on, kind, summary FROM episodes
           WHERE channel_id=? ORDER BY happened_on DESC, id DESC LIMIT ?""",
        (channel_id, limit)).fetchall()
    return [{"happened_on": r[0], "kind": r[1], "summary": r[2]}
            for r in rows]


def all_profiles(conn, limit=30):
    """全メンバーのプロファイル（得意分野マップRM#53の材料）。"""
    rows = conn.execute(
        """SELECT user_id, display_name, profile FROM profiles
           WHERE profile IS NOT NULL AND profile != ''
           ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
    return [{"user_id": r[0], "display_name": r[1], "profile": r[2]}
            for r in rows]


def upsert_prompt_variant(conn, *, slot, variant, body, created_at):
    conn.execute(
        """INSERT INTO prompt_variants(slot, variant, body, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(slot, variant) DO UPDATE SET body=excluded.body""",
        (slot, variant, body, created_at))


def active_variants(conn, slot):
    rows = conn.execute(
        """SELECT id, variant, body, used, up, down FROM prompt_variants
           WHERE slot=? AND active=1 ORDER BY id""", (slot,)).fetchall()
    return [{"id": r[0], "variant": r[1], "body": r[2], "used": r[3],
             "up": r[4], "down": r[5]} for r in rows]


def bump_variant_used(conn, variant_id):
    conn.execute(
        "UPDATE prompt_variants SET used = used + 1 WHERE id=?",
        (variant_id,))


def bump_variant_feedback(conn, variant_id, value):
    col = "up" if value == "up" else "down"
    conn.execute(
        f"UPDATE prompt_variants SET {col} = {col} + 1 WHERE id=?",
        (variant_id,))


def add_prophecy(conn, *, agent_id, period, payload_json, created_at):
    cur = conn.execute(
        """INSERT INTO prophecies(agent_id, period, payload_json, status,
               created_at) VALUES(?,?,?,'sealed',?)""",
        (agent_id, period, payload_json, created_at))
    return cur.lastrowid


def sealed_prophecy_before(conn, agent_id, period):
    """開封対象（前の期の封筒）。無ければ None。"""
    row = conn.execute(
        """SELECT id, period, payload_json FROM prophecies
           WHERE agent_id=? AND status='sealed' AND period < ?
           ORDER BY id ASC LIMIT 1""", (agent_id, period)).fetchone()
    return {"id": row[0], "period": row[1], "payload_json": row[2]} \
        if row else None


def prophecy_exists(conn, agent_id, period):
    return conn.execute(
        "SELECT 1 FROM prophecies WHERE agent_id=? AND period=? LIMIT 1",
        (agent_id, period)).fetchone() is not None


def open_prophecy(conn, prophecy_id, verdict_json, opened_at):
    conn.execute(
        """UPDATE prophecies SET status='opened', verdict_json=?, opened_at=?
           WHERE id=?""", (verdict_json, opened_at, prophecy_id))


def prophecy_accuracy(conn, agent_id):
    """開封済み予言の的中率（hit数/総数）。"""
    rows = conn.execute(
        """SELECT verdict_json FROM prophecies
           WHERE agent_id=? AND status='opened'""", (agent_id,)).fetchall()
    import json as _j
    hit = total = 0
    for (vj,) in rows:
        try:
            for v in _j.loads(vj or "[]"):
                total += 1
                hit += 1 if v.get("hit") else 0
        except ValueError:
            continue
    return {"hit": hit, "total": total}


def non_global_rules(conn, limit=20):
    """global以外のactiveルール（横展開の候補・RM#61）。"""
    rows = conn.execute(
        """SELECT id, agent_id, scope, rule_text FROM rules
           WHERE active=1 AND scope != 'global'
             AND id NOT IN (SELECT rule_id FROM share_proposals)
           ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    return [{"id": r[0], "agent_id": r[1], "scope": r[2], "rule_text": r[3]}
            for r in rows]


def add_share_proposal(conn, *, rule_id, from_agent, created_at):
    cur = conn.execute(
        """INSERT INTO share_proposals(rule_id, from_agent, status,
               created_at) VALUES(?,?,'proposed',?)""",
        (rule_id, from_agent, created_at))
    return cur.lastrowid


def set_share_message(conn, proposal_id, message_id):
    conn.execute(
        "UPDATE share_proposals SET proposal_message_id=? WHERE id=?",
        (message_id, proposal_id))


def share_proposal_by_message(conn, message_id):
    row = conn.execute(
        """SELECT id, rule_id, from_agent FROM share_proposals
           WHERE proposal_message_id=? AND status='proposed'""",
        (message_id,)).fetchone()
    return {"id": row[0], "rule_id": row[1], "from_agent": row[2]} \
        if row else None


def claim_share_proposal(conn, proposal_id, to_status):
    cur = conn.execute(
        """UPDATE share_proposals SET status=? WHERE id=? AND
           status='proposed'""", (to_status, proposal_id))
    return cur.rowcount == 1


def promote_rule_to_global(conn, rule_id):
    cur = conn.execute(
        "UPDATE rules SET scope='global' WHERE id=? AND active=1", (rule_id,))
    return cur.rowcount == 1


def bias_stats(conn, agent_id, since):
    """反応の偏り（RM#86・決定論）。誰の発言に・どのchで反応したか。"""
    people = conn.execute(
        """SELECT COALESCE(u.display_name, '不明'), COUNT(*)
           FROM proactive_log p
           LEFT JOIN messages m ON m.id = p.trigger_message_id
           LEFT JOIN users u ON u.id = m.author_id
           WHERE p.agent_id=? AND p.action='spoke' AND p.created_at >= ?
           GROUP BY 1 ORDER BY 2 DESC""", (agent_id, since)).fetchall()
    channels = conn.execute(
        """SELECT COALESCE(c.name, '不明'), COUNT(*)
           FROM proactive_log p
           LEFT JOIN channels c ON c.id = p.channel_id
           WHERE p.agent_id=? AND p.action='spoke' AND p.created_at >= ?
           GROUP BY 1 ORDER BY 2 DESC""", (agent_id, since)).fetchall()
    return {"people": [{"name": r[0], "n": r[1]} for r in people],
            "channels": [{"name": r[0], "n": r[1]} for r in channels]}


def audit_items_since(conn, agent_id, since):
    """自己監査日誌（RM#84）の材料: 怪しかった判断の当日分。"""
    rows = conn.execute(
        """SELECT kind, action, detail FROM proactive_log
           WHERE agent_id=? AND created_at >= ?
             AND ((kind='fake_done' AND action IN ('assert_flagged',
                                                   'caught'))
                  OR (kind='selfreview' AND action='score')
                  OR (action='silent' AND detail LIKE '懐疑役%'))
           ORDER BY id DESC LIMIT 20""", (agent_id, since)).fetchall()
    return [{"kind": r[0], "action": r[1], "detail": r[2] or ""}
            for r in rows]


def add_ripple_proposal(conn, *, decision_id, impacts_json, created_at):
    cur = conn.execute(
        """INSERT INTO ripple_proposals(decision_id, impacts_json, status,
               created_at) VALUES(?,?,'proposed',?)""",
        (decision_id, impacts_json, created_at))
    return cur.lastrowid


def set_ripple_message(conn, proposal_id, message_id):
    conn.execute(
        "UPDATE ripple_proposals SET proposal_message_id=? WHERE id=?",
        (message_id, proposal_id))


def ripple_by_message(conn, message_id):
    row = conn.execute(
        """SELECT id, decision_id, impacts_json FROM ripple_proposals
           WHERE proposal_message_id=? AND status='proposed'""",
        (message_id,)).fetchone()
    return {"id": row[0], "decision_id": row[1], "impacts_json": row[2]} \
        if row else None


def claim_ripple(conn, proposal_id, to_status):
    cur = conn.execute(
        """UPDATE ripple_proposals SET status=? WHERE id=? AND
           status='proposed'""", (to_status, proposal_id))
    return cur.rowcount == 1


def supersede_decision(conn, decision_id):
    """決定を上書き済み（superseded）にする（active のときだけ）。"""
    cur = conn.execute(
        "UPDATE decisions SET status='superseded' WHERE id=? AND "
        "status='active'", (decision_id,))
    return cur.rowcount == 1


def decisions_after(conn, after_id, limit=5):
    """波及チェック対象の新しい決定（古い順）。"""
    rows = conn.execute(
        """SELECT id, decision, topic, channel_id, source_message_id
           FROM decisions WHERE id > ? AND status='active'
           ORDER BY id ASC LIMIT ?""", (after_id, limit)).fetchall()
    return [{"id": r[0], "decision": r[1], "topic": r[2],
             "channel_id": r[3], "source_message_id": r[4]} for r in rows]


def all_open_action_items(conn, limit=20):
    """全エージェント横断のopenタスク（波及チェック用）。"""
    rows = conn.execute(
        """SELECT id, task, owners, due_date FROM action_items
           WHERE status='open' ORDER BY due_date ASC LIMIT ?""",
        (limit,)).fetchall()
    return [{"id": r[0], "task": r[1], "owners": r[2], "due_date": r[3]}
            for r in rows]


def planned_events(conn, limit=10):
    rows = conn.execute(
        """SELECT id, name, event_date FROM events
           WHERE status='planned' ORDER BY event_date ASC LIMIT ?""",
        (limit,)).fetchall()
    return [{"id": r[0], "name": r[1], "event_date": r[2]} for r in rows]


def upsert_wiki_page(conn, *, topic, channel_id, message_id,
                     last_decision_id, created_by, now):
    conn.execute(
        """INSERT INTO wiki_pages(topic, channel_id, message_id,
               last_decision_id, created_by, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(topic) DO UPDATE SET channel_id=excluded.channel_id,
               message_id=excluded.message_id,
               last_decision_id=excluded.last_decision_id,
               updated_at=excluded.updated_at""",
        (topic, channel_id, message_id, last_decision_id, created_by,
         now, now))


def wiki_pages_all(conn):
    rows = conn.execute(
        """SELECT id, topic, channel_id, message_id, last_decision_id
           FROM wiki_pages ORDER BY id""").fetchall()
    return [{"id": r[0], "topic": r[1], "channel_id": r[2],
             "message_id": r[3], "last_decision_id": r[4]} for r in rows]


def touch_wiki_page(conn, page_id, last_decision_id, now):
    conn.execute(
        "UPDATE wiki_pages SET last_decision_id=?, updated_at=? WHERE id=?",
        (last_decision_id, now, page_id))


def max_decision_id(conn):
    row = conn.execute("SELECT COALESCE(MAX(id),0) FROM decisions").fetchone()
    return row[0]


def user_prev_message_gap(conn, user_id, before_message_id):
    """浦島パック（#102）: 指定メッセージの直前にあるその人の発言時刻。
    Returns: (直前発言のcreated_at, 対象メッセージのcreated_at) or None。"""
    cur_row = conn.execute(
        "SELECT created_at FROM messages WHERE id=?",
        (before_message_id,)).fetchone()
    if not cur_row:
        return None
    prev = conn.execute(
        """SELECT created_at FROM messages
           WHERE author_id=? AND id < ? AND deleted=0
           ORDER BY id DESC LIMIT 1""",
        (user_id, before_message_id)).fetchone()
    if not prev:
        return None
    return prev[0], cur_row[0]


def get_agent_session(conn, agent_id, channel_id):
    row = conn.execute(
        """SELECT session_id, turns, started_at, last_used_at
           FROM agent_sessions WHERE agent_id=? AND channel_id=?""",
        (agent_id, channel_id)).fetchone()
    return {"session_id": row[0], "turns": row[1], "started_at": row[2],
            "last_used_at": row[3]} if row else None


def save_agent_session(conn, *, agent_id, channel_id, session_id, turns,
                       started_at, last_used_at):
    conn.execute(
        """INSERT INTO agent_sessions(agent_id, channel_id, session_id,
               turns, started_at, last_used_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(agent_id, channel_id) DO UPDATE SET
               session_id=excluded.session_id, turns=excluded.turns,
               started_at=excluded.started_at,
               last_used_at=excluded.last_used_at""",
        (agent_id, channel_id, session_id, turns, started_at, last_used_at))


def clear_agent_session(conn, agent_id, channel_id):
    conn.execute(
        "DELETE FROM agent_sessions WHERE agent_id=? AND channel_id=?",
        (agent_id, channel_id))


def get_watch_hash(conn, alias, tab):
    row = conn.execute(
        "SELECT content_hash FROM sheet_watch_state WHERE alias=? AND tab=?",
        (alias, tab)).fetchone()
    return row[0] if row else None


def set_watch_hash(conn, alias, tab, content_hash, updated_at):
    conn.execute(
        """INSERT INTO sheet_watch_state(alias, tab, content_hash, updated_at)
           VALUES(?,?,?,?) ON CONFLICT(alias, tab) DO UPDATE SET
               content_hash=excluded.content_hash,
               updated_at=excluded.updated_at""",
        (alias, tab, content_hash, updated_at))


def _deadline_row(r):
    return {"id": r[0], "alias": r[1], "tab": r[2], "item_key": r[3],
            "name": r[4], "due_date": r[5], "channel_id": r[6],
            "stage": r[7], "batch_message_id": r[8]}


_DEADLINE_COLS = ("id, alias, tab, item_key, name, due_date, channel_id, "
                  "stage, batch_message_id")


def active_sheet_deadlines(conn, alias, tab):
    rows = conn.execute(
        f"""SELECT {_DEADLINE_COLS} FROM sheet_deadlines
            WHERE alias=? AND tab=? AND status='active' ORDER BY due_date""",
        (alias, tab)).fetchall()
    return [_deadline_row(r) for r in rows]


def all_active_sheet_deadlines(conn):
    rows = conn.execute(
        f"""SELECT {_DEADLINE_COLS} FROM sheet_deadlines
            WHERE status='active' ORDER BY due_date""").fetchall()
    return [_deadline_row(r) for r in rows]


def add_sheet_deadline(conn, *, alias, tab, item_key, name, due_date,
                       channel_id, created_at):
    """期日行の追加。過去に削除済みの同名行があれば復活させる。"""
    conn.execute(
        """INSERT INTO sheet_deadlines(alias, tab, item_key, name, due_date,
               channel_id, stage, status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,0,'active',?,?)
           ON CONFLICT(alias, tab, item_key) DO UPDATE SET
               due_date=excluded.due_date, channel_id=excluded.channel_id,
               stage=0, status='active', updated_at=excluded.updated_at""",
        (alias, tab, item_key, name, due_date, channel_id,
         created_at, created_at))


def reschedule_sheet_deadline(conn, alias, tab, item_key, due_date,
                              updated_at):
    """期日変更: 通知段階をリセットして新しい日付で追跡し直す。"""
    conn.execute(
        """UPDATE sheet_deadlines SET due_date=?, stage=0, updated_at=?
           WHERE alias=? AND tab=? AND item_key=? AND status='active'""",
        (due_date, updated_at, alias, tab, item_key))


def cancel_sheet_deadline(conn, alias, tab, item_key, updated_at):
    conn.execute(
        """UPDATE sheet_deadlines SET status='cancelled', updated_at=?
           WHERE alias=? AND tab=? AND item_key=? AND status='active'""",
        (updated_at, alias, tab, item_key))


def set_sheet_deadline_stage(conn, row_id, stage, updated_at):
    conn.execute(
        "UPDATE sheet_deadlines SET stage=?, updated_at=? WHERE id=?",
        (stage, updated_at, row_id))


def set_sheet_deadline_batch(conn, alias, tab, message_id):
    """直近の同期で動いた行に報告メッセージidを紐づける（❌一括取消用）。"""
    conn.execute(
        """UPDATE sheet_deadlines SET batch_message_id=?
           WHERE alias=? AND tab=? AND status='active'""",
        (message_id, alias, tab))


def cancel_sheet_deadlines_by_batch(conn, message_id, updated_at):
    cur = conn.execute(
        """UPDATE sheet_deadlines SET status='cancelled', updated_at=?
           WHERE batch_message_id=? AND status='active'""",
        (updated_at, message_id))
    return cur.rowcount


def set_sheet_watch(conn, alias, agent_id, watch_json):
    cur = conn.execute(
        """UPDATE sheet_registry SET watch=? WHERE alias=? AND agent_id=?
           AND active=1""", (watch_json, alias, agent_id))
    return cur.rowcount > 0


def watched_sheets(conn, agent_id):
    """watch設定のあるactiveな登録シート。
    Returns: [{"alias","spreadsheet_id","watches":[{"tab","channel_id"}]}]"""
    rows = conn.execute(
        """SELECT alias, spreadsheet_id, watch FROM sheet_registry
           WHERE agent_id=? AND active=1 AND watch IS NOT NULL""",
        (agent_id,)).fetchall()
    out = []
    for alias, sid, wjson in rows:
        try:
            watches = json.loads(wjson or "[]")
        except ValueError:
            continue
        if watches:
            out.append({"alias": alias, "spreadsheet_id": sid,
                        "watches": watches})
    return out


def add_fact(conn, *, agent_id, topic, fact, source_kind, source_message_id,
             channel_id, stated_by, created_at):
    """事実を1件記録。同じtopicの既存activeは superseded にする（最新が正）。
    Returns: (新id, 上書きした件数)"""
    cur = conn.execute(
        """UPDATE facts SET status='superseded'
           WHERE topic=? AND status='active'""", (topic,))
    superseded = cur.rowcount
    cur = conn.execute(
        """INSERT INTO facts(agent_id, topic, fact, source_kind,
               source_message_id, channel_id, stated_by, status, created_at)
           VALUES(?,?,?,?,?,?,?,'active',?)""",
        (agent_id, topic, fact, source_kind, source_message_id, channel_id,
         stated_by, created_at))
    return cur.lastrowid, superseded


def search_facts(conn, keywords, limit=8):
    """事実をキーワードLIKEで検索（activeのみ・新しい順）。
    キーワードが空/短すぎなら直近limit件を返す（決定台帳と同じ契約）。"""
    base = ("SELECT id, topic, fact, source_message_id, channel_id, "
            "stated_by, created_at FROM facts WHERE status='active'")
    kws = [k.strip() for k in (keywords or []) if len(k.strip()) >= 2][:8]
    if kws:
        clauses = " OR ".join("(fact LIKE ? OR topic LIKE ?)" for _ in kws)
        params = []
        for k in kws:
            params += [f"%{k}%", f"%{k}%"]
        rows = conn.execute(
            base + f" AND ({clauses}) ORDER BY id DESC LIMIT ?",
            params + [limit]).fetchall()
    else:
        rows = conn.execute(base + " ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{"id": r[0], "topic": r[1], "fact": r[2],
             "source_message_id": r[3], "channel_id": r[4],
             "stated_by": r[5], "created_at": r[6]} for r in rows]


def cancel_fact(conn, fact_id):
    cur = conn.execute(
        "UPDATE facts SET status='cancelled' WHERE id=? AND status='active'",
        (fact_id,))
    return cur.rowcount == 1


def count_facts(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()
    return row[0]


def proactive_hit_stats(conn, agent_id):
    """自発発言の累計的中率の原料（RM#50）: 発言総数・👍つき・👎つき。
    無反応も分母に入れる＝正直な数字で自己開示する。"""
    row = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN EXISTS(SELECT 1 FROM feedback f
                       WHERE f.message_id=p.posted_message_id
                         AND f.value='up') THEN 1 ELSE 0 END),
                  SUM(CASE WHEN EXISTS(SELECT 1 FROM feedback f
                       WHERE f.message_id=p.posted_message_id
                         AND f.value='down') THEN 1 ELSE 0 END)
           FROM proactive_log p
           WHERE p.agent_id=? AND p.action='spoke'""",
        (agent_id,)).fetchone()
    return {"spoke": row[0] or 0, "up": row[1] or 0, "down": row[2] or 0}


def count_proactive_log_since(conn, kind, action, since):
    """proactive_logの種別別カウント（週次レポート用の汎用集計）。"""
    return conn.execute(
        """SELECT COUNT(*) FROM proactive_log
           WHERE kind=? AND action=? AND created_at >= ?""",
        (kind, action, since)).fetchone()[0]


def count_correction_rules_since(conn, since):
    """訂正から学んだルール（rule_textが「訂正:」始まり）の件数（RM#17）。"""
    return conn.execute(
        """SELECT COUNT(*) FROM rules
           WHERE rule_text LIKE '訂正:%' AND created_at >= ?""",
        (since,)).fetchone()[0]


def proactive_spoke_by_posted(conn, message_id):
    """自発発言（spoke）の記録を投稿message_idから引く（RM#7の👎フック用）。"""
    row = conn.execute(
        """SELECT agent_id, kind, channel_id FROM proactive_log
           WHERE posted_message_id=? AND action='spoke'""",
        (message_id,)).fetchone()
    if row is None:
        return None
    return {"agent_id": row[0], "kind": row[1], "channel_id": row[2]}


def add_proactive_lesson(conn, *, agent_id, kind, channel_id, message_id,
                         text, created_at, polarity="down"):
    """教訓を記録（同一投稿は1件だけ）。新規に入れたら True。
    polarity: down=👎の失敗例 / up=👍の勝ちパターン / advice=自己採点の蒸留"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO proactive_lessons(agent_id, kind, channel_id,
               message_id, text, active, created_at, polarity)
           VALUES(?,?,?,?,?,1,?,?)""",
        (agent_id, kind, channel_id, message_id, text, created_at, polarity))
    return cur.rowcount == 1


def deactivate_proactive_lesson(conn, message_id, polarity="down"):
    """リアクションが全部外れた投稿の教訓を無効化する。
    polarity指定: 同じ投稿でも👎由来と👍由来を別々に解除できるように。"""
    conn.execute(
        "UPDATE proactive_lessons SET active=0 WHERE message_id=? AND polarity=?",
        (message_id, polarity))


def recent_proactive_lessons(conn, agent_id, limit=5, polarity="down"):
    """新しい順のactiveな教訓（プロンプトへの注入用）。"""
    rows = conn.execute(
        """SELECT kind, text FROM proactive_lessons
           WHERE agent_id=? AND active=1 AND polarity=?
           ORDER BY id DESC LIMIT ?""",
        (agent_id, polarity, limit)).fetchall()
    return [{"kind": r[0], "text": r[1]} for r in rows]


def replace_advice_lessons(conn, agent_id, texts, created_at):
    """自己採点の蒸留結果を差し替える（古い助言を無効化→新しい助言を登録）。
    週次で最新の蒸留だけが生きる＝助言が無限に積もらない。"""
    conn.execute(
        "UPDATE proactive_lessons SET active=0 WHERE agent_id=? AND polarity='advice'",
        (agent_id,))
    for text in texts:
        conn.execute(
            """INSERT INTO proactive_lessons(agent_id, kind, channel_id,
                   message_id, text, active, created_at, polarity)
               VALUES(?, 'selfreview', NULL, NULL, ?, 1, ?, 'advice')""",
            (agent_id, text, created_at))


def count_downs_for_message(conn, message_id):
    """ある投稿に残っている👎の数（教訓の解除判定用）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE message_id=? AND value='down'",
        (message_id,)).fetchone()[0]


def count_ups_for_message(conn, message_id):
    """ある投稿に残っている👍の数（勝ちパターンの解除判定用）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE message_id=? AND value='up'",
        (message_id,)).fetchone()[0]


def selfreview_scores_since(conn, agent_id, since):
    """自己採点の記録（RM#14）を新しい順に返す。detail は 'スコア|問題点'。"""
    rows = conn.execute(
        """SELECT detail, created_at FROM proactive_log
           WHERE agent_id=? AND kind='selfreview' AND action='score'
             AND created_at >= ?
           ORDER BY id DESC""",
        (agent_id, since)).fetchall()
    return [{"detail": r[0], "created_at": r[1]} for r in rows]


def proactive_feedback_stats(conn, agent_id, since):
    """自発発言への👍👎を (kind, channel_id) 単位で集計（RM#11
    リアクション自動学習の原料）。since以降の発言が対象。"""
    rows = conn.execute(
        """SELECT p.kind, p.channel_id,
                  SUM(CASE WHEN f.value='up' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN f.value='down' THEN 1 ELSE 0 END)
           FROM proactive_log p
           JOIN feedback f ON f.message_id = p.posted_message_id
           WHERE p.agent_id=? AND p.action='spoke' AND p.created_at >= ?
           GROUP BY p.kind, p.channel_id""", (agent_id, since)).fetchall()
    return [{"kind": r[0], "channel_id": r[1], "up": r[2] or 0,
             "down": r[3] or 0} for r in rows]


def add_sheet(conn, *, alias, spreadsheet_id, title, mode, agent_id,
              registered_by, created_at):
    """シート台帳（起票#3）に1件登録し、採番されたidを返す。
    同一エージェント内のalias重複はUNIQUE制約で拒否される
    （呼び出し側が get_sheet_by_alias で先に確認する）。"""
    cur = conn.execute(
        """INSERT INTO sheet_registry(alias, spreadsheet_id, title, mode,
                                      agent_id, registered_by, active,
                                      created_at)
           VALUES(?,?,?,?,?,?,1,?)""",
        (alias, spreadsheet_id, title, mode, agent_id, registered_by,
         created_at))
    return cur.lastrowid


def _sheet_row(r):
    return {"id": r[0], "alias": r[1], "spreadsheet_id": r[2], "title": r[3],
            "mode": r[4], "watch": r[5]}


def get_sheet_by_alias(conn, alias, agent_id):
    """activeな登録シート1件（無ければNone）。alias→ID解決の唯一の経路。"""
    row = conn.execute(
        """SELECT id, alias, spreadsheet_id, title, mode, watch
           FROM sheet_registry WHERE alias=? AND agent_id=? AND active=1""",
        (alias, agent_id)).fetchone()
    return None if row is None else _sheet_row(row)


def list_sheets(conn, agent_id):
    """エージェントのactiveな登録シートを古い順に返す。"""
    rows = conn.execute(
        """SELECT id, alias, spreadsheet_id, title, mode, watch
           FROM sheet_registry WHERE agent_id=? AND active=1
           ORDER BY id ASC""", (agent_id,)).fetchall()
    return [_sheet_row(r) for r in rows]


def deactivate_sheet(conn, alias, agent_id):
    """登録を解除。対象があれば title を返す（無ければ None）。"""
    row = conn.execute(
        """SELECT id, title FROM sheet_registry
           WHERE alias=? AND agent_id=? AND active=1""",
        (alias, agent_id)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE sheet_registry SET active=0 WHERE id=?", (row[0],))
    return row[1]


def stats(conn):
    msg = conn.execute("SELECT COUNT(*) FROM messages WHERE deleted=0").fetchone()[0]
    att = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    ch = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    rules = conn.execute("SELECT COUNT(*) FROM rules WHERE active=1").fetchone()[0]
    return {"messages": msg, "attachments": att, "channels": ch,
            "active_rules": rules}
