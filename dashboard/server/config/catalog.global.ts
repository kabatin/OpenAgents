/**
 * 全体設定のカタログ（config.json のトップレベル ＋ dev_bot ＋ 議事録BOTの別config）。
 *
 * 対応するコード:
 *   chatbot/agent_runtime.py 53-124行（トップレベル）
 *   devbot/bot.py load_dev_config / DevBot.__init__（dev_bot.*）
 *   platforms/discord/meeting/bot.py（config.json の meeting_bot）
 */
import { WEEKDAY_OPTIONS, type Setting, type SettingGroup } from "./types.ts";

const CONVERSATION: Setting[] = [
  {
    path: "guild_id",
    label: "対象のDiscordサーバー",
    desc: "AIが動作する唯一のサーバーID。ここ以外のサーバーの発言はすべて無視されます。",
    kind: "string",
    readonly: true,
  },
  {
    path: "context_history_limit",
    label: "会話をさかのぼる件数",
    desc: "返答するときに「直前の会話」として何件読むか。多いほど話の流れを覚えていますが、遅く高価になります。",
    kind: "int",
    default: 30,
    min: 1,
    max: 200,
    unit: "件",
  },
  {
    path: "max_bot_chain",
    label: "AI同士の連続発言の上限",
    desc: "AI同士が続けて喋れる回数。これに達すると自動で黙り、無限ループを止めます（人間が発言するとリセット）。",
    kind: "int",
    default: 4,
    min: 1,
    max: 20,
    unit: "回",
  },
  {
    path: "max_concurrent_answers",
    label: "同時に考えてよい件数",
    desc: "AIが同時に処理できる回答数。増やすと反応が速くなりますが、マシンが重くなります。",
    kind: "int",
    default: 2,
    min: 1,
    max: 10,
    unit: "件",
  },
  {
    path: "history_limit",
    label: "（非推奨）履歴の窓",
    desc: "単独では使われず、上の「会話をさかのぼる件数」の下限としてのみ効きます。触る必要はありません。",
    kind: "int",
    default: 10,
    min: 1,
    max: 200,
    unit: "件",
    readonly: true,
  },
  {
    path: "admins",
    label: "管理者のDiscordユーザーID",
    desc: "全体ルールの設定・他人のリマインダー削除・AI採用の承認ができる人。",
    kind: "stringList",
    default: [],
  },
  {
    path: "agent_category_id",
    label: "新AI用チャンネルのカテゴリ",
    desc: "AI人事が新しいAI用のチャンネルを作るときの入れ物カテゴリID。",
    kind: "string",
    default: null,
    readonly: true,
    fixedNote:
      "DiscordのIDは構造そのものなので画面からは変更できません（変更はconfig.jsonを直接編集）",
  },
];

const LLM: Setting[] = [
  {
    path: "llm.provider",
    label: "使うAI（LLM）",
    desc:
      "回答を作るのに使うコマンドラインAI。インストール済みのものだけ選べます。" +
      "添付ファイルの読解など一部の機能は Claude Code でしか動きません。",
    kind: "enum",
    default: "claude",
    options: [
      { value: "claude", label: "Claude Code（claude）" },
      { value: "codex", label: "Codex CLI（codex）" },
      { value: "custom", label: "自分で指定したコマンド" },
    ],
  },
  {
    path: "llm.model",
    label: "モデル名",
    desc: "上で選んだAIに渡すモデル名。分からなければ既定のままで構いません。",
    kind: "string",
    default: "claude-sonnet-5",
  },
  {
    path: "llm.timeout_sec",
    label: "応答を待つ時間",
    desc: "AIの返事を何秒待つか。長い資料を読ませるときは長めにします。",
    kind: "int",
    default: 180,
    min: 10,
    max: 1800,
    unit: "秒",
  },
];

const INTEGRATIONS: Setting[] = [
  {
    path: "integrations.enabled",
    label: "有効にする外部連携",
    desc:
      "integrations/ フォルダに入れた連携のうち、実際に読み込むものの名前を並べます。" +
      "書き方は docs/07-integrations.md にあります。",
    kind: "stringList",
    default: [],
  },
];

const DASHBOARD: Setting[] = [
  {
    path: "dashboard.host",
    label: "この画面を開ける範囲",
    desc:
      "127.0.0.1 なら自分のPCからだけ。0.0.0.0 にすると同じLANの他の端末からも開けます" +
      "（その場合はパスワードを必ず設定してください）。インターネットには公開しないでください。",
    kind: "enum",
    default: "127.0.0.1",
    options: [
      { value: "127.0.0.1", label: "自分のPCだけ（推奨）" },
      { value: "0.0.0.0", label: "同じLANの端末からも開く" },
    ],
  },
  {
    path: "dashboard.port",
    label: "ポート番号",
    desc: "この画面を開くときのポート。既定は 8787 です。",
    kind: "int",
    default: 8787,
    min: 1024,
    max: 65535,
  },
  {
    path: "dashboard.password",
    label: "画面のパスワード",
    desc:
      "設定するとこの画面を開くときにパスワードを聞かれます（ユーザー名は admin）。" +
      "LANに公開するなら必須です。",
    kind: "string",
    secret: true,
    default: "",
  },
];

const DEV_BOT: Setting[] = [
  {
    path: "dev_bot.enabled",
    label: "開発BOT",
    desc: "Discordから開発を指示して、テストを通ったものだけを承認つきで反映できます。既定はオフです。",
    kind: "bool",
    default: false,
  },
  {
    path: "dev_bot.token",
    label: "Botトークン",
    desc: "開発BOTがDiscordに接続するための鍵。ここに貼ると保存されますが、保存後は読み出せません（画面には ●●●● と出ます）。",
    kind: "string",
    secret: true,
  },
  {
    path: "dev_bot.dev_channel_id",
    label: "開発用チャンネル",
    desc: "開発BOTが常駐して、監視の報告と開発指示の受付をするチャンネル。",
    kind: "string",
  },
  {
    path: "dev_bot.monitor.interval_sec",
    label: "監視の間隔",
    desc: "何秒おきに他のBOTの生死を確認するか。この周期で生存証明（heartbeat）も更新されます。",
    kind: "int",
    default: 60,
    min: 10,
    max: 600,
    unit: "秒",
  },
  {
    path: "dev_bot.monitor.stall_after_sec",
    label: "無音とみなす時間",
    desc: "ログが何秒動かなければ「止まっている」とみなすか。監視対象ごとに無音検知をオンにしたときだけ効きます。",
    kind: "int",
    default: 1800,
    min: 60,
    max: 86400,
    unit: "秒",
  },
  {
    path: "dev_bot.weekly_report.enabled",
    label: "開発の週次レポート",
    desc: "その週に実装・デプロイしたものをまとめて報告します。",
    kind: "bool",
    default: false,
    children: [
      {
        path: "dev_bot.weekly_report.weekday",
        label: "報告する曜日",
        desc: "レポートを出す曜日。",
        kind: "weekday",
        default: 4,
        options: WEEKDAY_OPTIONS,
      },
      {
        path: "dev_bot.weekly_report.hour",
        label: "報告する時刻",
        desc: "レポートを出す時刻。",
        kind: "hour",
        default: 18,
      },
      {
        path: "dev_bot.weekly_report.channel_id",
        label: "報告先チャンネル",
        desc: "未設定ならAI開発室に出します。",
        kind: "string",
        default: null,
      },
    ],
  },
];

/** 議事録BOT。パスは config.json の meeting_bot セクション基準。 */
const MEETING_BOT: Setting[] = [
  {
    path: "enabled",
    label: "議事録BOT",
    desc: "ボイスチャンネルの会話を録音して、文字起こしと議事録を作ります。音声の追加インストールが必要です（docs/05-autostart.md）。",
    kind: "bool",
    default: false,
  },
  {
    path: "token",
    label: "Botトークン",
    desc: "議事録BOT用のDiscord Botトークン。会話用のエージェントとは別のBotを作って登録します。",
    kind: "string",
    secret: true,
  },
  {
    path: "voice_channel_name",
    label: "録音する音声チャンネル",
    desc: "この名前のボイスチャンネルに人が集まると、議事録BOTが録音を始めます。",
    kind: "string",
  },
  {
    path: "user_mapping",
    label: "話者名のマッピング",
    desc: "Discordのユーザー名から、議事録に載せるメンション表記への対応表。新メンバーが入ったらここに追加します。",
    kind: "info",
    fixedNote: "編集は下の一覧から行えます",
  },
];

export const GLOBAL_GROUPS: SettingGroup[] = [
  {
    id: "conversation",
    label: "会話と上限",
    desc: "全エージェント共通の基本パラメータ。",
    settings: CONVERSATION,
  },
  { id: "llm", label: "使うAI（LLM）", settings: LLM },
  { id: "integrations", label: "外部連携", settings: INTEGRATIONS },
  { id: "dashboard", label: "この管理画面", settings: DASHBOARD },
];

export const DEV_BOT_GROUPS: SettingGroup[] = [
  {
    id: "devbot",
    label: "開発BOT",
    desc: "別プロセスで動くため、ここの変更は開発BOTだけを再起動します。",
    settings: DEV_BOT,
  },
];

export const MEETING_BOT_GROUPS: SettingGroup[] = [
  {
    id: "meetingbot",
    label: "議事録BOT",
    desc: "AIエージェントではありませんが、同じ仕組みで常駐しています。既定はオフです。",
    settings: MEETING_BOT,
  },
];
