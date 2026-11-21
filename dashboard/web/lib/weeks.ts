/**
 * 週ごとの集計で使う、週の区切り（月曜始まり・純粋関数）。
 * 日付は JST の YYYY-MM-DD 文字列で受け渡しする。
 */

export const WEEKS_DEFAULT = 4;

const DAY_MS = 86_400_000;

function parseDay(day: string | null | undefined): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(day ?? "");
  if (m === null) return null;
  const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(ms) ? null : ms;
}

function isoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** その日が入る週の月曜（月曜始まり）。日付として読めなければ null。 */
export function weekStart(day: string | null | undefined): string | null {
  const ms = parseDay(day);
  if (ms === null) return null;
  const sinceMonday = (new Date(ms).getUTCDay() + 6) % 7;
  return isoDay(ms - sinceMonday * DAY_MS);
}

/** today の週から、さかのぼって weeks 週ぶんの月曜（新しい週が先）。 */
export function recentWeeks(today: string, weeks: number): string[] {
  const thisWeek = parseDay(weekStart(today));
  if (thisWeek === null || weeks <= 0) return [];
  return Array.from({ length: weeks }, (_, i) =>
    isoDay(thisWeek - i * 7 * DAY_MS),
  );
}
