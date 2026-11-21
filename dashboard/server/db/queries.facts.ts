/**
 * 事実台帳（「いまの状況」の記録）の読み取り。
 * 書き手は core/facts.py（教わったもの・会話から拾ったもの）。
 * 取り消しは Discord の会話から行う（ここは見るだけ）。
 */
import { safeQuery } from "./ro.ts";

export type FactRow = {
  id: number;
  agentId: string | null;
  topic: string | null;
  fact: string | null;
  /** "extracted"＝エージェントが会話から拾った／それ以外＝人が教えた */
  sourceKind: string | null;
  /** active / superseded（新しい事実に置き換わった）/ cancelled（取り消した） */
  status: string | null;
  statedBy: string | null;
  channelName: string | null;
  createdAt: string | null;
};

const FACT_LIMIT = 200;

/** いま効いているものを先に、その中では新しい順。 */
export function facts(): FactRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[number], FactRow>(
          `SELECT f.id            AS id,
                  f.agent_id      AS agentId,
                  f.topic         AS topic,
                  f.fact          AS fact,
                  f.source_kind   AS sourceKind,
                  f.status        AS status,
                  f.stated_by     AS statedBy,
                  c.name          AS channelName,
                  f.created_at    AS createdAt
             FROM facts f
             LEFT JOIN channels c ON c.id = f.channel_id
            ORDER BY (f.status = 'active') DESC, f.id DESC
            LIMIT ?`,
        )
        .all(FACT_LIMIT),
    [],
  );
}
