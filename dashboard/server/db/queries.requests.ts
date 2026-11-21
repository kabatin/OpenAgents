/**
 * 人に頼まれた回答の記録の読み取り。週ごとの集計は画面側の
 * web/lib/requests.ts が行う。質問文は返さない（数えるのに要らない）。
 * 書き手は platforms/discord/tool_loop.py（回答のたびに kind=tool_loop を残す）。
 */
import { safeQuery, todayJst } from "./ro.ts";

export type RequestLogRow = {
  day: string | null;
  action: string;
  tools: string;
  who: string | null;
};

/** 画面が出す週数より長めに読む（週数を増やしても読み直さずに済む）。 */
const LOOKBACK_DAYS = 12 * 7;

function daysBefore(day: string, n: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) - n * 86_400_000)
    .toISOString()
    .slice(0, 10);
}

/** detail は「道具,道具 | 脚注」。脚注には発言の中身が入りうるので返さない。 */
export function toolsOf(detail: string | null): string {
  return (detail ?? "").split(" | ")[0]?.trim() ?? "";
}

/**
 * 人に頼まれた回答の記録。proactive_log.created_at は素のJST文字列で、
 * today も JST で返す。Bot 起点のターンは「頼まれた」ではないので除く。
 */
export function requestLog(): { today: string; rows: RequestLogRow[] } {
  const today = todayJst();
  const rows = safeQuery(
    (conn) =>
      conn
        .prepare<
          [string],
          { day: string | null; action: string; detail: string | null; who: string | null }
        >(
          `SELECT substr(p.created_at, 1, 10) AS day, p.action, p.detail,
                  COALESCE(u.display_name, u.name) AS who
             FROM proactive_log p
             LEFT JOIN messages m ON m.id = p.trigger_message_id
             LEFT JOIN users u    ON u.id = m.author_id
            WHERE p.kind = 'tool_loop'
              AND p.created_at >= ?
              AND COALESCE(u.is_bot, 0) = 0
            ORDER BY p.id`,
        )
        .all(daysBefore(today, LOOKBACK_DAYS)),
    [],
  );
  return {
    today,
    rows: rows.map((r) => ({
      day: r.day,
      action: r.action,
      tools: toolsOf(r.detail),
      who: r.who,
    })),
  };
}
