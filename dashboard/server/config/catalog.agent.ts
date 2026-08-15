/**
 * エージェント1体ぶんの設定カタログ（基本動作・スキル）。
 * パスは config.json の `agents[]` の1要素を基準にした相対パス。
 *
 * 対応するコード: chatbot/bot.py `AgentClient.__init__`（113-152行）
 */
import type { Setting, SettingGroup } from "./types.ts";
import { PROACTIVE_GROUPS } from "./catalog.proactive.ts";

const BASICS: Setting[] = [
  {
    path: "name",
    label: "表示名",
    desc: "Discordでの名前。プロンプトにも埋め込まれるので、変えると自己紹介の内容も変わります。",
    kind: "string",
  },
  {
    path: "role",
    label: "担当分野の説明",
    desc: "このAIが何の担当かを一文で。同僚一覧・組織図・引き継ぎ判断に使われます。",
    kind: "text",
    default: "",
  },
  {
    path: "token",
    label: "Botトークン",
    desc: "Discordに接続するための鍵。ここに貼ると保存されますが、保存後は読み出せません（画面には ●●●● と出ます）。",
    kind: "string",
    secret: true,
  },
  {
    path: "home_channel_id",
    label: "ホームチャンネル",
    desc: "このAIの自室。ここでは（下の設定次第で）呼ばれなくても答えます。",
    kind: "string",
    readonly: true,
  },
  {
    path: "persona_files",
    label: "人格ファイル",
    desc: "口調と人格を定義しているMarkdownファイル。中身の編集はエディタで行ってください。",
    kind: "stringList",
    readonly: true,
  },
  {
    path: "archiver",
    label: "アーカイブ保存係",
    desc: "全チャンネルの会話をDBに保存する係。必ずちょうど1体だけがこの役目を持ちます。",
    kind: "bool",
    default: false,
    readonly: true,
    fixedNote: "2体以上／0体になるとBOTが起動しなくなるため画面からは変更できません",
  },
  {
    path: "require_mention",
    label: "呼ばれた時だけ答える",
    desc: "オンにすると、自室でも「@名前」で呼ばれた時だけ返事します。チャンネルを静かに保ちたい子に。",
    kind: "bool",
    default: false,
  },
  {
    path: "runner_enabled",
    label: "新しい回答経路を使う",
    desc: "Web検索やツールを使える新しい経路で回答します。オフにすると旧経路（ツール無し）に戻ります。この経路は Claude Code 専用で、他のAIを選んでいるときは自動で旧経路になります。",
    kind: "bool",
    default: true,
  },
  {
    path: "self_review.enabled",
    label: "回答の自己採点",
    desc: "投稿した自分の回答を後からAIが採点して記録します。投稿内容には影響しない裏方の計測です。",
    kind: "bool",
    default: false,
    fixedNote: "100文字未満の短い回答は採点しません",
  },
  {
    path: "peer_review",
    label: "納品後にレビューを頼む相手",
    desc: "画像などを納品したあと、指定した同僚AIに一言レビューをもらいます。",
    kind: "enum",
    default: null,
    options: [
      { value: null, label: "頼まない" },
      { value: "agent1", label: "エージェント1" },
      { value: "agent2", label: "エージェント2" },
      { value: "agent3", label: "エージェント3" },
    ],
  },
  {
    path: "session_resume.enabled",
    label: "会話の続きを覚える",
    desc: "同じチャンネルでのやりとりを「続き」として記憶し続けます。添付ファイルがあるターンは対象外です。「新しい回答経路」がオフのときは一緒にオフになります。",
    kind: "bool",
    default: true,
    requires: [{ path: "runner_enabled", label: "新しい回答経路" }],
    children: [
      {
        path: "session_resume.hot_minutes",
        label: "続きとみなす間隔",
        desc: "前回から何分以内なら同じ会話として続けるか。過ぎたら仕切り直します。",
        kind: "int",
        default: 60,
        min: 1,
        max: 1440,
        unit: "分",
      },
      {
        path: "session_resume.max_turns",
        label: "続ける往復の上限",
        desc: "同じ会話を何往復まで続けるか。超えたら作り直します。",
        kind: "int",
        default: 20,
        min: 1,
        max: 200,
        unit: "往復",
      },
      {
        path: "session_resume.max_age_hours",
        label: "会話の寿命",
        desc: "古い記憶が腐るのを防ぐための上限時間。",
        kind: "int",
        default: 6,
        min: 1,
        max: 168,
        unit: "時間",
      },
    ],
  },
  {
    path: "thread_reply",
    label: "スレッドで返信する",
    desc: "返信をチャンネルに直接ではなく、元の投稿のスレッドの中に入れます。チャンネルが流れにくくなります。",
    kind: "tri",
    default: { enabled: false, shadow: true },
    children: [
      {
        path: "thread_reply.flows",
        label: "スレッド化する対象",
        desc: "mention=通常の応答 / pdf=PDFの自動要約。",
        kind: "stringList",
        default: ["mention", "pdf"],
      },
    ],
  },
];

const SKILLS: Setting[] = [
  {
    path: "skills.reminder",
    label: "リマインダー",
    desc: "「明日9時に◯◯を知らせて」のような自然な言葉でリマインダーを登録し、時間が来たら配信します。",
    kind: "bool",
    default: false,
  },
  {
    path: "skills.youtube_summary",
    label: "YouTube要約",
    desc: "YouTubeのURLが貼られたら、字幕から内容を要約して返します。",
    kind: "bool",
    default: false,
  },
  {
    path: "skills.pdf_summary",
    label: "PDFの自動要約",
    desc: "PDFが添付されたら、呼ばれていなくても自動で要約します。",
    kind: "bool",
    default: false,
  },
  {
    // image_gen は「設定オブジェクトの存在＝有効」。トグルは .enabled リーフに
    // 書き（既存のbackend等を保全）、表示は presenceIsOn で解決する
    path: "skills.image_gen.enabled",
    label: "画像生成",
    desc: "依頼に応じて画像を生成して投稿します。参考画像を添付すると、そのテイストを踏まえて作ります。",
    kind: "bool",
    default: false,
    presenceIsOn: true,
    children: [
      {
        path: "skills.image_gen.backend",
        label: "使う外部連携",
        desc: "画像を作る本体は同梱していません。integrations/ に入れた連携の名前を書きます（docs/07-integrations.md）。",
        kind: "string",
        default: "imagegen",
      },
      {
        path: "skills.image_gen.timeout_sec",
        label: "生成を待つ時間",
        desc: "1枚の生成を何秒待つか。",
        kind: "int",
        default: 900,
        min: 30,
        max: 3600,
        unit: "秒",
      },
    ],
  },
];

export function agentGroups(): SettingGroup[] {
  return [
    { id: "basics", label: "基本動作", settings: BASICS },
    {
      id: "skills",
      label: "スキル",
      desc: "呼ばれたとき／条件を満たしたときに働く能力。",
      settings: SKILLS,
    },
    ...PROACTIVE_GROUPS,
  ];
}
