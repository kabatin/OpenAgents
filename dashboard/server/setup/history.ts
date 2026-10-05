/**
 * 過去ログをどこから取り込むか（設定 archive.since）。
 *
 * 昔から運用されている大きなサーバーで全履歴を取り込むと、数時間〜数日かかり
 * DBも膨らむ。セットアップで「取り込まない／直近N日／すべて」を選んでもらい、
 * その起点を保存する。読む側は core/archive_window.py（形式はそちらと揃える）。
 */

export type HistoryMode = "none" | "days" | "all";

export const HISTORY_DAYS_DEFAULT = 90;
export const HISTORY_DAYS_MAX = 3650;

const JST_OFFSET_MS = 9 * 60 * 60 * 1000;
const pad = (n: number) => String(n).padStart(2, "0");

/** 日本時間の暦（UTCのgetterで読めるようにずらした Date）。 */
function jst(d: Date): Date {
  return new Date(d.getTime() + JST_OFFSET_MS);
}

function jstDate(d: Date): string {
  const j = jst(d);
  return `${j.getUTCFullYear()}-${pad(j.getUTCMonth() + 1)}-${pad(j.getUTCDate())}`;
}

/**
 * 選択から archive.since の値を作る。null は「すべて」（キーを持たない）。
 * - none: セットアップした時刻（以後のやりとりだけを覚える）
 * - days: 日本時間で N 日前の0時（日付だけで書く）
 */
export function sinceFromChoice(mode: HistoryMode, days: number | undefined, now: Date): string | null {
  if (mode === "all") return null;
  if (mode === "none") {
    const j = jst(now);
    return (
      `${jstDate(now)}T${pad(j.getUTCHours())}:${pad(j.getUTCMinutes())}:` +
      `${pad(j.getUTCSeconds())}+09:00`
    );
  }
  if (mode === "days") {
    const n = Number(days);
    if (!Number.isInteger(n) || n < 1 || n > HISTORY_DAYS_MAX) {
      throw new Error(`日数は 1〜${HISTORY_DAYS_MAX} の整数で指定してください`);
    }
    return jstDate(new Date(now.getTime() - n * 24 * 60 * 60 * 1000));
  }
  throw new Error("取り込み方の指定が読めません（none / days / all）");
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const DATETIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$/;

/** core/archive_window.parse_since が読める形式か（未設定・空は「すべて」で有効）。 */
export function isValidSince(v: unknown): boolean {
  if (v === null || v === undefined || v === "") return true;
  if (typeof v !== "string") return false;
  const m = DATE_RE.exec(v);
  if (m) {
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    return d.getUTCMonth() === Number(m[2]) - 1 && d.getUTCDate() === Number(m[3]);
  }
  return DATETIME_RE.test(v) && !Number.isNaN(Date.parse(v));
}
