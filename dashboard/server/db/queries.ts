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

/**
 * このアーカイブがどのDiscordサーバーの記録か（未記録なら null）。
 *
 * messages/channels は guild_id を持たないため、繋ぎ先だけ別サーバーに
 * 変えると前のサーバーの会話が消せないまま残る。選ぶ前に警告するのに使う。
 */
export function archivedGuildId(): string | null {
  return safeQuery((conn) => {
    const row = conn
      .prepare<[], { value: string | null }>(
        `SELECT value FROM meta WHERE key = 'archive_guild_id'`,
      )
      .get();
    return row?.value ?? null;
  }, null);
}

/** 記録されている会話の件数（0なら消しても失うものが無い）。 */
export function archivedMessageCount(): number {
  return safeQuery(
    (conn) =>
      conn.prepare<[], { n: number }>(`SELECT COUNT(*) AS n FROM messages`).get()?.n ?? 0,
    0,
  );
}

export type ObservationRow = {
  agentId: string;
  kind: string;
  action: string;
  channel: string | null;
  author: string | null;
  trigger: string | null;
  detail: string | null;
  createdAt: string | null;
  channelId: number | null;
  triggerMessageId: number | null;
};

/**
 * 同僚枠の記録。実験時はシャドー（kind='others'・投稿なし）で、
 * ⑤colleague として本採用したあとは実際に投稿される（kind='colleague'）。
 * action で spoke（発言した）/ shadow（記録のみ）/ silent（コードが止めた）
 * を見分ける。
 */
export function observationShadow(limit = 100): ObservationRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[number], ObservationRow>(
          `SELECT p.agent_id AS agentId, p.kind AS kind, p.action AS action,
                  c.name AS channel,
                  u.display_name AS author,
                  SUBSTR(m.content, 1, 160) AS trigger,
                  p.detail AS detail, p.created_at AS createdAt,
                  p.channel_id AS channelId,
                  p.trigger_message_id AS triggerMessageId
             FROM proactive_log p
             LEFT JOIN channels c ON c.id = p.channel_id
             LEFT JOIN messages m ON m.id = p.trigger_message_id
             LEFT JOIN users u ON u.id = m.author_id
            WHERE (p.kind = 'others' AND p.action = 'shadow')
               OR (p.kind = 'colleague')
            ORDER BY p.id DESC LIMIT ?`,
        )
        .all(limit),
    [],
  );
}

export type AdviceRow = {
  id: number;
  agentId: string;
  text: string;
  streak: number;
  createdAt: string | null;
};

/** 回答に常時注入されている自己改善メモ（週次蒸留の結果・streakは連続週数）。 */
export function adviceLessons(): AdviceRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], AdviceRow>(
          `SELECT id, agent_id AS agentId, text,
                  COALESCE(streak, 1) AS streak, created_at AS createdAt
             FROM proactive_lessons
            WHERE polarity = 'advice' AND active = 1
            ORDER BY streak DESC, id DESC`,
        )
        .all(),
    [],
  );
}

export type TermRow = {
  term: string;
  description: string | null;
  createdBy: string | null;
  createdAt: string | null;
};

/**
 * 固有名詞辞書。正式表記そのものを覚えさせるもので、
 * 「音が近い未知の誤変換」もLLMがここへ寄せる（db.py の terms）。
 */
export function terms(): TermRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], TermRow>(
          `SELECT term, description, created_by AS createdBy,
                  created_at AS createdAt
             FROM terms ORDER BY created_at DESC`,
        )
        .all(),
    [],
  );
}

export type GlossaryRow = {
  wrong: string;
  correct: string | null;
  createdBy: string | null;
  createdAt: string | null;
};

/** 単語帳（誤→正の置換ペア）。固有名詞辞書と違い、決定論的に置換される。 */
export function glossary(): GlossaryRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], GlossaryRow>(
          `SELECT wrong, correct, created_by AS createdBy,
                  created_at AS createdAt
             FROM glossary ORDER BY created_at DESC`,
        )
        .all(),
    [],
  );
}

// ---------------------------------------------------------------- ゴールデン（模範Q&A）

export type GoldenRow = {
  id: number;
  agentId: string | null;
  kind: string | null;
  status: string | null;
  question: string | null;
  answer: string | null;
  sourceLink: string | null;
  note: string | null;
  createdAt: string | null;
};

/** 模範Q&A。kind=curated は人が採用/不採用を決める候補、auto は👍の自動捕獲。 */
export function goldenRows(limit = 200): GoldenRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[number], GoldenRow>(
          `SELECT id, agent_id AS agentId, COALESCE(kind, 'auto') AS kind, status,
                  question, answer, source_link AS sourceLink, note,
                  created_at AS createdAt
             FROM golden_set
            ORDER BY CASE status WHEN 'candidate' THEN 0 WHEN 'curated' THEN 1 ELSE 2 END,
                     id DESC
            LIMIT ?`,
        )
        .all(limit),
    [],
  );
}

/** 自発ログの最大ID（SSEで「それ以降の新着」を取るためのカーソル） */
// ---------------------------------------------------------------- LLM呼び出し台帳（v4 Phase 0）

export type LlmDailyRow = {
  day: string;
  calls: number;
  failed: number;
  costUsd: number;
  avgMs: number | null;
  maxMs: number | null;
  cacheReadTokens: number;
  outputTokens: number;
};

export type LlmPurposeRow = {
  agentId: string | null;
  purpose: string | null;
  calls: number;
  failed: number;
  costUsd: number;
  avgMs: number | null;
};

export type LlmCallRow = {
  id: number;
  agentId: string | null;
  purpose: string | null;
  model: string | null;
  ok: number | null;
  error: string | null;
  durationMs: number | null;
  wallMs: number | null;
  numTurns: number | null;
  costUsd: number | null;
  toolCalls: number | null;
  denials: number | null;
  createdAt: string | null;
};

/** created_at（"YYYY-MM-DDTHH:MM"・JST naive）と比較できる「N日前の0時」 */
function sinceStamp(days: number): string {
  const d = new Date(Date.now() + 9 * 3600 * 1000 - days * 86400 * 1000);
  return `${d.toISOString().slice(0, 10)}T00:00`;
}

/** 日別: 呼び出し数・失敗・コスト・所要（claude CLI の result イベント由来）。 */
export function llmDaily(days = 14): LlmDailyRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[string], LlmDailyRow>(
          `SELECT substr(created_at, 1, 10) AS day,
                  COUNT(*) AS calls,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,
                  COALESCE(SUM(cost_usd), 0) AS costUsd,
                  CAST(AVG(duration_ms) AS INTEGER) AS avgMs,
                  MAX(duration_ms) AS maxMs,
                  COALESCE(SUM(cache_read_tokens), 0) AS cacheReadTokens,
                  COALESCE(SUM(output_tokens), 0) AS outputTokens
             FROM llm_calls WHERE created_at >= ?
            GROUP BY day ORDER BY day DESC`,
        )
        .all(sinceStamp(days)),
    [],
  );
}

/** 用途別（直近N日）: どの機能がいくら使い、どれだけ失敗しているか。 */
export function llmByPurpose(days = 7): LlmPurposeRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[string], LlmPurposeRow>(
          `SELECT agent_id AS agentId, purpose,
                  COUNT(*) AS calls,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,
                  COALESCE(SUM(cost_usd), 0) AS costUsd,
                  CAST(AVG(duration_ms) AS INTEGER) AS avgMs
             FROM llm_calls WHERE created_at >= ?
            GROUP BY agent_id, purpose ORDER BY costUsd DESC`,
        )
        .all(sinceStamp(days)),
    [],
  );
}

/** 直近の呼び出し（失敗の理由を読むため error 込み）。 */
export function llmRecent(limit = 60): LlmCallRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[number], LlmCallRow>(
          `SELECT id, agent_id AS agentId, purpose, model, ok, error,
                  duration_ms AS durationMs, wall_ms AS wallMs,
                  num_turns AS numTurns, cost_usd AS costUsd,
                  tool_calls AS toolCalls, denials, created_at AS createdAt
             FROM llm_calls ORDER BY id DESC LIMIT ?`,
        )
        .all(limit),
    [],
  );
}

export function maxActivityId(): number {
  return safeQuery(
    (conn) => conn.prepare<[], { n: number | null }>(`SELECT MAX(id) AS n FROM proactive_log`).get()?.n ?? 0,
    0,
  );
}

// ---------------------------------------------------------------- 追跡タスクの統一ビュー

export type TaskRow = {
  key: string;
  kind: "action" | "homework" | "reminder";
  id: number;
  task: string | null;
  owner: string | null;
  due: string | null;
  status: string | null;
  stage: string | null;
  channelId: number | null;
};

/** 議事録TODO・宿題を1つの形で（リマインダーは reminders.json 側で合流）。 */
export function tasksUnified(): TaskRow[] {
  return safeQuery(
    (conn) =>
      conn
        .prepare<[], TaskRow>(
          `SELECT 'A' || id AS key, 'action' AS kind, id, task, owners AS owner,
                  due_date AS due, status, nudge_stage AS stage, channel_id AS channelId
             FROM action_items WHERE status IN ('open','stale')
           UNION ALL
           SELECT 'H' || id, 'homework', id, task, owner, follow_up_date, status, status,
                  channel_id
             FROM homework_items WHERE status IN ('open','asked','asked2')
           ORDER BY due ASC, key ASC`,
        )
        .all(),
    [],
  );
}
