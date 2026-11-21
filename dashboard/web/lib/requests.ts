/**
 * 「人に頼まれたか・道具を使って応えたか」を週ごとに数える（純粋関数）。
 *
 * 投稿数や稼働時間は「動いた量」で、使われているかは分からない。
 * 元データは BOT が回答のたびに残す依頼の記録（proactive_log の kind=tool_loop）。
 */
import { recentWeeks, weekStart, WEEKS_DEFAULT } from "./weeks.ts";

/** 依頼の記録1件。day は JST の日付、tools は使った道具（カンマ区切り）。 */
export type RequestRow = {
  day: string | null;
  action: string;
  tools: string;
  who: string | null;
};

export type WeekCount = {
  /** 頼まれた数 */
  asked: number;
  /** うち、道具（検索・登録など）を使って応えた数 */
  acted: number;
  /** 頼んだ人数（名前の分かる人だけ） */
  people: number;
};

export type RequestSummary = {
  /** 各週の月曜（YYYY-MM-DD）。新しい週が先 */
  weeks: string[];
  byWeek: WeekCount[];
  total: WeekCount;
  /** 種類ごとの件数（多い順）。byWeek は weeks と同じ並び */
  categories: { id: string; asked: number; byWeek: number[] }[];
  /** 頼んだ人ごとの件数（多い順） */
  people: { name: string; asked: number }[];
};

/**
 * 道具の名前 → 種類。上から順に当てる。連携プラグイン（integrations）が足した
 * 道具など、ここに無いものは「その他」。
 */
const RULES: readonly (readonly [RegExp, string])[] = [
  [/_reminders?$/, "reminder"],
  [/^update_task$/, "task"],
  [/^save_|^cancel_(fact|rule)$/, "teach"],
  [
    /^(search_messages|get_facts|get_decisions|lookup_terms|recall_lessons|list_tasks)$/,
    "recall",
  ],
];

function categoryOfTool(tool: string): string {
  return RULES.find(([re]) => re.test(tool))?.[1] ?? "other";
}

/**
 * 1件の依頼の種類。調べもの（recall）は他の操作の下調べにも使うので、
 * ほかの種類の道具が1つでもあればそちらで数える。
 */
export function categoryOf(tools: string): string {
  const cats = tools
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t !== "")
    .map(categoryOfTool);
  if (cats.length === 0) return "none";
  return cats.find((c) => c !== "recall") ?? "recall";
}

/** 数える対象か。シャドーは同じ依頼の試し書きなので数えない。 */
function counted(action: string): boolean {
  return action === "used" || action === "unused";
}

function tally(rows: readonly RequestRow[]): WeekCount {
  return {
    asked: rows.length,
    acted: rows.filter((r) => r.action === "used").length,
    people: new Set(rows.flatMap((r) => (r.who === null ? [] : [r.who]))).size,
  };
}

function countBy<T>(items: readonly T[], key: (item: T) => string | null) {
  return items.reduce<Record<string, number>>((acc, item) => {
    const k = key(item);
    return k === null ? acc : { ...acc, [k]: (acc[k] ?? 0) + 1 };
  }, {});
}

/** today（JST・YYYY-MM-DD）の週から、さかのぼって weeks 週ぶんを集計する。 */
export function summarizeRequests(
  rows: readonly RequestRow[],
  today: string,
  weeks: number = WEEKS_DEFAULT,
): RequestSummary {
  const weekList = recentWeeks(today, weeks);
  const inRange = rows
    .filter((r) => counted(r.action))
    .map((r) => ({ ...r, week: weekStart(r.day) }))
    .filter((r) => r.week !== null && weekList.includes(r.week));
  const byCategory = countBy(inRange, (r) => categoryOf(r.tools));
  const categories = Object.entries(byCategory)
    .map(([id, asked]) => ({
      id,
      asked,
      byWeek: weekList.map(
        (w) =>
          inRange.filter((r) => r.week === w && categoryOf(r.tools) === id)
            .length,
      ),
    }))
    .sort((a, b) => b.asked - a.asked || a.id.localeCompare(b.id));
  const people = Object.entries(countBy(inRange, (r) => r.who))
    .map(([name, asked]) => ({ name, asked }))
    .sort((a, b) => b.asked - a.asked || a.name.localeCompare(b.name));
  return {
    weeks: weekList,
    byWeek: weekList.map((w) => tally(inRange.filter((r) => r.week === w))),
    total: tally(inRange),
    categories,
    people,
  };
}
