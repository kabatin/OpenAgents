/**
 * ダッシュボードが使う読み取りクエリ。
 *
 * カラム名は chatbot/db.py のスキーマに実際に当てて検証済み。
 * 時刻はすべて素のJST文字列（`YYYY-MM-DDTHH:MM`）で入っているため、
 * 比較には date('now','+9 hours') を使う（messages テーブルだけUTCなので注意）。
 */
import { safeQuery, todayJst } from "./ro.ts";

export type QuotaRow = { agentId: string; used: number; dbOverride: number | null };

/**
 * 本日の自発発言数。`action='spoke'` だけが枠を消費する
 * （nudge / track は別勘定 — db.py count_proactive_spoken_since のコメントに準拠）。
 */
export function todayQuota(agentIds: string[]): QuotaRow[] {
  return safeQuery((conn) => {
    const used = conn
      .prepare<[string], { agent_id: string; used: number }>(
        `SELECT agent_id, COUNT(*) AS used
           FROM proactive_log
          WHERE action = 'spoke' AND substr(created_at, 1, 10) = ?
          GROUP BY agent_id`,
      )
      .all(todayJst());
    const overrides = conn
      .prepare<[], { agent_id: string; daily_quota: number }>(
        `SELECT agent_id, daily_quota FROM proactive_settings`,
      )
      .all();
    const usedMap = new Map(used.map((r) => [r.agent_id, r.used]));
    const overrideMap = new Map(overrides.map((r) => [r.agent_id, r.daily_quota]));
    return agentIds.map((agentId) => ({
      agentId,
      used: usedMap.get(agentId) ?? 0,
      dbOverride: overrideMap.get(agentId) ?? null,
    }));
  }, agentIds.map((agentId) => ({ agentId, used: 0, dbOverride: null })));
}

export type LastRunRow = { agentId: string; lastRunAt: string | null };

/** メイン観察ループの最終実行時刻。名前空間つきのキー（`minutes:agent1` 等）は除く。 */
export function lastRuns(agentIds: string[]): LastRunRow[] {
  return safeQuery((conn) => {
    const rows = conn
      .prepare<[], { agent_id: string; last_run_at: string | null }>(
        `SELECT agent_id, last_run_at FROM proactive_state WHERE instr(agent_id, ':') = 0`,
      )
      .all();
    const map = new Map(rows.map((r) => [r.agent_id, r.last_run_at]));
    return agentIds.map((agentId) => ({ agentId, lastRunAt: map.get(agentId) ?? null }));
  }, agentIds.map((agentId) => ({ agentId, lastRunAt: null })));
}

export type SubLoopRow = { loop: string; scope: string; lastRunAt: string | null };

/** サブループ（minutes / capwatch / audit / sheetwatch / comeback…）の最終実行。 */
export function subLoops(): SubLoopRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], SubLoopRow>(
          `SELECT substr(agent_id, 1, instr(agent_id, ':') - 1) AS loop,
                  substr(agent_id, instr(agent_id, ':') + 1)     AS scope,
                  last_run_at                                    AS lastRunAt
             FROM proactive_state
            WHERE instr(agent_id, ':') > 0
            ORDER BY last_run_at DESC`,
        )
        .all(),
    [],
  );
}

export type ActivityRow = {
  id: number;
  createdAt: string;
  agentId: string;
  kind: string;
  action: string;
  channelId: number | null;
  channelName: string | null;
  detail: string | null;
  postedMessageId: number | null;
};

/**
 * 自発行動のタイムライン。
 * `action='silent'`（黙ると判断した記録）が全体の8割を占めるので、
 * 既定では除外して「実際に何かした」ものだけを見せる。
 */
export function recentActivity(opts: { limit?: number; sinceId?: number; includeSilent?: boolean } = {}): ActivityRow[] {
  const limit = Math.min(Math.max(opts.limit ?? 50, 1), 500);
  const sinceId = opts.sinceId ?? 0;
  const silentClause = opts.includeSilent ? "" : "AND p.action <> 'silent'";
  return safeQuery(
    (conn) =>
      conn
        .prepare<[number, number], ActivityRow>(
          `SELECT p.id                AS id,
                  p.created_at        AS createdAt,
                  p.agent_id          AS agentId,
                  p.kind              AS kind,
                  p.action            AS action,
                  p.channel_id        AS channelId,
                  c.name              AS channelName,
                  p.detail            AS detail,
                  p.posted_message_id AS postedMessageId
             FROM proactive_log p
             LEFT JOIN channels c ON c.id = p.channel_id
            WHERE p.id > ? ${silentClause}
            ORDER BY p.id DESC
            LIMIT ?`,
        )
        .all(sinceId, limit),
    [],
  );
}

export type Counters = {
  activeRules: number;
  capabilityRequests: { status: string; count: number }[];
  roadmap: { status: string; count: number }[];
  feedback: { agentId: string; up: number; down: number }[];
  webhookAgents: { id: string; name: string; status: string; homeChannelId: number | null }[];
  sheetRegistry: { alias: string; title: string | null; mode: string; active: number }[];
  messages: number;
  channels: number;
};

export function counters(): Counters {
  const empty: Counters = {
    activeRules: 0,
    capabilityRequests: [],
    roadmap: [],
    feedback: [],
    webhookAgents: [],
    sheetRegistry: [],
    messages: 0,
    channels: 0,
  };
  return safeQuery((conn) => {
    const nowJst = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 16);
    const activeRules =
      conn
        .prepare<[string], { n: number }>(
          `SELECT COUNT(*) AS n FROM rules
            WHERE active = 1 AND (expires_at IS NULL OR expires_at > ?)`,
        )
        .get(nowJst)?.n ?? 0;

    return {
      activeRules,
      capabilityRequests: conn
        .prepare<[], { status: string; count: number }>(
          `SELECT status, COUNT(*) AS count FROM capability_requests GROUP BY status`,
        )
        .all(),
      roadmap: conn
        .prepare<[], { status: string; count: number }>(
          `SELECT status, COUNT(*) AS count FROM roadmap_items
            GROUP BY status ORDER BY COUNT(*) DESC`,
        )
        .all(),
      feedback: conn
        .prepare<[], { agentId: string; up: number; down: number }>(
          `SELECT agent_id AS agentId,
                  SUM(value = 'up')   AS up,
                  SUM(value = 'down') AS down
             FROM feedback WHERE kind = 'reaction' GROUP BY agent_id`,
        )
        .all(),
      webhookAgents: conn
        .prepare<[], { id: string; name: string; status: string; homeChannelId: number | null }>(
          `SELECT id, name, status, home_channel_id AS homeChannelId
             FROM agents ORDER BY status, id`,
        )
        .all(),
      sheetRegistry: conn
        .prepare<[], { alias: string; title: string | null; mode: string; active: number }>(
          `SELECT alias, title, mode, active FROM sheet_registry ORDER BY alias`,
        )
        .all(),
      messages: conn.prepare<[], { n: number }>(`SELECT COUNT(*) AS n FROM messages`).get()?.n ?? 0,
      channels: conn.prepare<[], { n: number }>(`SELECT COUNT(*) AS n FROM channels`).get()?.n ?? 0,
    };
  }, empty);
}

export type RuleRow = {
  id: number;
  agentId: string;
  scope: string;
  ruleText: string;
  createdBy: string | null;
  active: number;
  createdAt: string;
  expiresAt: string | null;
};

export function rules(): RuleRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], RuleRow>(
          `SELECT id, agent_id AS agentId, scope, rule_text AS ruleText,
                  created_by AS createdBy, active, created_at AS createdAt,
                  expires_at AS expiresAt
             FROM rules ORDER BY active DESC, id DESC`,
        )
        .all(),
    [],
  );
}

export type CapabilityRow = {
  id: number;
  agentId: string;
  description: string;
  requestedBy: string | null;
  status: string;
  createdAt: string;
};

export function capabilityRequests(): CapabilityRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], CapabilityRow>(
          `SELECT id, agent_id AS agentId, description, requested_by AS requestedBy,
                  status, created_at AS createdAt
             FROM capability_requests ORDER BY id DESC`,
        )
        .all(),
    [],
  );
}

export type RoadmapRow = {
  id: number;
  title: string;
  category: string | null;
  route: string | null;
  status: string;
  createdAt: string;
  decidedAt: string | null;
};

export function roadmapItems(): RoadmapRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], RoadmapRow>(
          `SELECT id, title, category, route, status, created_at AS createdAt,
                  decided_at AS decidedAt
             FROM roadmap_items ORDER BY id DESC`,
        )
        .all(),
    [],
  );
}

export type DevJobRow = {
  id: number;
  capReqId: number | null;
  branch: string | null;
  status: string;
  summary: string | null;
  createdAt: string;
  updatedAt: string;
};

export function devJobs(): DevJobRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], DevJobRow>(
          `SELECT id, cap_req_id AS capReqId, branch, status, summary,
                  created_at AS createdAt, updated_at AS updatedAt
             FROM dev_jobs ORDER BY id DESC LIMIT 50`,
        )
        .all(),
    [],
  );
}

export type DeployRow = {
  jobId: number;
  capReqId: number | null;
  files: string | null;
  deployedAt: string;
  revertedAt: string | null;
  canaryStatus: string | null;
};

export function deployHistory(): DeployRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], DeployRow>(
          `SELECT job_id AS jobId, cap_req_id AS capReqId, files,
                  deployed_at AS deployedAt, reverted_at AS revertedAt,
                  canary_status AS canaryStatus
             FROM deploy_history ORDER BY deployed_at DESC LIMIT 50`,
        )
        .all(),
    [],
  );
}

/** 自発ログの最大ID（SSEで「それ以降の新着」を取るためのカーソル） */
export function maxActivityId(): number {
  return safeQuery(
    (conn) => conn.prepare<[], { n: number | null }>(`SELECT MAX(id) AS n FROM proactive_log`).get()?.n ?? 0,
    0,
  );
}
