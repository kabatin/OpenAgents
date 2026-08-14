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
  nudge: "納期の声かけ",
  track: "追跡を宣言",
  cancel: "追跡を会話で取消",
  done: "追跡を会話で完了",
  score: "自己採点",
  used: "ツールを使用",
  breached: "訓練で突破された",
  caught: "嘘を検知して訂正",
  distilled: "教訓を蒸留",
  published: "発行した",
  event_proposed: "イベント案を提示",
  rescue_shadow: "救援（シャドー）",
  prep_shadow: "事前パック（シャドー）",
  stale_shadow: "状況確認（シャドー）",
};

export const KIND_LABEL: Record<string, string> = {
  none: "定期観察",
  selfreview: "自己採点",
  selfreview_distill: "自己採点の蒸留",
  handoff: "引き継ぎ",
  recall: "記憶の呼び出し",
  comeback: "浦島あらすじ",
  deadline: "期日",
  plugin: "プラグイン",
  rescue: "見捨てられた質問",
  drill: "乗っ取り訓練",
  info: "情報提供",
  outreach: "御用聞き",
  assist: "手助け",
  contradiction: "矛盾の指摘",
  event: "イベント",
  fake_done: "完了の偽り",
  news: "ニュース",
  newspaper: "社内新聞",
  prep: "事前パック",
  ripple: "波紋",
  stale: "停滞",
};

export function actionLabel(a: string): string {
  return ACTION_LABEL[a] ?? a;
}
export function kindLabel(k: string): string {
  return KIND_LABEL[k] ?? k;
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
