import type { HealthStatus } from "./types.ts";

const WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"];

export function weekdayLabel(v: unknown): string {
  return typeof v === "number" && WEEKDAYS[v] !== undefined ? `${WEEKDAYS[v]}曜` : "—";
}

export function hourLabel(v: unknown): string {
  return typeof v === "number" ? `${String(v).padStart(2, "0")}:00` : "—";
}

export function bytes(n: number | null): string {
  if (n === null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function relTime(sec: number | null): string {
  if (sec === null) return "—";
  if (sec < 60) return `${Math.floor(sec)}秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}分前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}時間前`;
  return `${Math.floor(sec / 86400)}日前`;
}

/** DBの素のJST文字列（YYYY-MM-DDTHH:MM）を読みやすくする。 */
export function jstStamp(raw: string | null): string {
  if (raw === null) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(raw);
  if (!m) return raw;
  const [, , mo, d, h, mi] = m;
  const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
  return raw.slice(0, 10) === today ? `${h}:${mi}` : `${mo}/${d} ${h}:${mi}`;
}

export function since(ms: number | null): string {
  if (ms === null) return "—";
  return relTime((Date.now() - ms) / 1000);
}

export const STATUS_TONE: Record<HealthStatus, { dot: string; text: string; chip: string }> = {
  ok: { dot: "bg-accent", text: "text-accent-deep", chip: "bg-accent-soft text-accent-deep" },
  down: { dot: "bg-danger", text: "text-danger", chip: "bg-danger-soft text-danger" },
  disconnected: { dot: "bg-warn", text: "text-warn", chip: "bg-warn-soft text-warn" },
  stalled: { dot: "bg-warn", text: "text-warn", chip: "bg-warn-soft text-warn" },
  idle: { dot: "bg-faint", text: "text-muted", chip: "bg-canvas text-muted" },
  unknown: { dot: "bg-faint", text: "text-muted", chip: "bg-canvas text-muted" },
};

/** 自発ログの action / kind を日本語に。 */
export const ACTION_LABEL: Record<string, string> = {
  spoke: "発言した",
  silent: "黙った",
  stale: "追跡を手放した",
  reopen: "追跡を再開",
  cycle_timeout: "サイクルを打ち切り",
  nudge: "納期の声かけ",
  nudge_shadow: "納期の声かけ（投稿しない設定）",
  track: "追跡を宣言",
  cancel: "追跡を会話で取消",
  done: "追跡を会話で完了",
  score: "自己採点",
  used: "ツールを使用",
  unused: "ツールを使わず回答",
  shadow: "シャドーで記録",
  denied: "権限外のツール呼び出し",
  breached: "訓練で突破された",
  caught: "嘘を検知して訂正",
  distilled: "教訓を蒸留",
  published: "発行した",
  event_proposed: "イベント案を提示",
  rescue_shadow: "救援（シャドー）",
  prep_shadow: "事前パック（シャドー）",
  stale_shadow: "状況確認（シャドー）",
  hw_track: "宿題を追跡開始",
  hw_shadow: "宿題の声かけ（シャドー）",
  hw_nudge: "宿題の声かけ",
  hw_nudge2: "宿題の二度目の声かけ",
  hw_closed: "宿題を流れたものとして手放した",
  hw_resolved: "宿題は会話で完了済みと判断",
  attention_tracked: "気になる話題を様子見",
  attention_shadow: "自発介入（シャドー）",
  attention_spoke: "会話に口を挟んだ",
  attention_resolved: "人間だけで解決したので取り下げ",
};

export const KIND_LABEL: Record<string, string> = {
  none: "会話の見守り",
  namecall: "名前で呼ばれた",
  tool_loop: "ツールで調べた",
  tool_denied: "権限外のツール呼び出し",
  selfreview: "自己採点",
  selfreview_distill: "自己採点のまとめ",
  handoff: "他のエージェントへの引き継ぎ",
  recall: "過去の話の呼び出し",
  comeback: "不在明けのまとめ",
  deadline: "議事録TODOの期日",
  plugin: "プラグイン",
  rescue: "放置された質問への回答",
  drill: "乗っ取り訓練",
  info: "情報提供",
  outreach: "御用聞き",
  assist: "手助け",
  contradiction: "矛盾の指摘",
  event: "イベント",
  fake_done: "できたフリの検出",
  news: "ニュース",
  newspaper: "社内新聞",
  prep: "定例の事前パック",
  ripple: "決定の波及チェック",
  stale: "放置されたチャンネル",
  homework: "宿題（「やっときます」）",
  attention: "会話への口出し",
};

export function actionLabel(a: string): string {
  return ACTION_LABEL[a] ?? a;
}
export function kindLabel(k: string): string {
  return KIND_LABEL[k] ?? k;
}

/**
 * Discordの該当投稿へのジャンプURL。
 * 「タイムラインで妙な発言を見つけたら、その場で現物を開く」ための導線。
 * 必要なIDが揃わないときは null（リンクにしない）。
 */
export function discordUrl(
  guildId: string | null,
  channelId: string | null,
  messageId: string | null,
): string | null {
  if (guildId === null || channelId === null || messageId === null) return null;
  return `https://discord.com/channels/${guildId}/${channelId}/${messageId}`;
}

/**
 * 画面に出る内部ステータスの日本語化。
 * `curated` / `drafted` / `overdue` のような英語の内部値をそのまま人間に見せない。
 * 知らない値は素通しする（勝手な訳を当てない）。
 */
const JA: Record<string, string> = {
  // ゴールデン（模範Q&A）
  candidate: "採用待ち",
  curated: "採用",
  active: "自動捕獲",
  rejected: "不採用",
  invalid: "無効",
  shadow: "シャドー",
  // 納期追跡の声かけ段階
  before: "期日前",
  overdue: "期日超過",
  none: "声かけ前",
  stale: "放置されたチャンネル",
  open: "対応中",
  done: "完了",
  // LLM呼び出しの用途
  answer: "回答",
  keywords: "キーワード抽出",
  screen: "一次判定",
  decide: "二次判定",
  summary: "要約",
  self_review: "自己採点",
  distill: "蒸留",
  audit: "自己点検",
  skeptic: "発言前の監査",
  attention: "会話への口出し",
  rescue: "救援",
  briefing: "ブリーフィング",
  homework: "宿題（「やっときます」）",
  bg_task: "裏の作業",
  other: "その他",
};

export function ja(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return JA[value] ?? value;
}

/** 日付だけの値（`2026-09-24`）を `09/24` に。時刻つきは jstStamp を使う。 */
export function jstDate(raw: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === "") return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  return m === null ? raw : `${m[2]}/${m[3]}`;
}

/**
 * エージェントIDを表示名にする。
 *
 * **固定の対応表は持たない。** エージェントは利用者が自由に増やせるので、
 * 名前は必ず設定から来る。names に無いIDは、そのままIDを見せる
 * （知らない名前を勝手に作らない）。
 */
export function agentLabel(id: string, names?: Record<string, string>): string {
  return names?.[id] ?? (id === "devbot" ? "開発BOT" : id);
}

/** Discord の書式（-# 小文字行・メンション・太字）を一覧で読める文字にする。 */
export function plainDiscord(text: string | null | undefined): string {
  return (text ?? "")
    .replace(/^-# /gm, "")
    .replace(/<@&?\d+>/g, "@…")
    .replace(/<#\d+>/g, "#…")
    .replace(/\*\*/g, "")
    .trim();
}
