/** データページで使う API の型（server/db/queries.ts・routes/api.ts の返り値）。 */

export type Summary = {
  counters: {
    activeRules: number;
    capabilityRequests: { status: string; count: number }[];
    roadmap: { status: string; count: number }[];
    feedback: { agentId: string; up: number; down: number }[];
    webhookAgents: {
      id: string;
      name: string;
      status: string;
      homeChannelId: string | null;
    }[];
    sheetRegistry: {
      alias: string;
      title: string | null;
      mode: string;
      active: number;
    }[];
    messages: number;
    channels: number;
  };
  reminders: { active: number; error: number; total: number };
};

export type Rule = {
  id: number;
  agentId: string;
  scope: string;
  scopeName?: string | null;
  ruleText: string;
  createdBy: string | null;
  active: number;
  createdAt: string;
  expiresAt: string | null;
};

export type Capability = {
  id: number;
  agentId: string;
  description: string;
  status: string;
  createdAt: string;
};

export type Reminder = {
  id: number;
  user_name: string;
  content: string;
  due: string;
  repeat: string;
  status: string;
  channel_label?: string;
  agent_id: string;
};

export type TermEntry = {
  term: string;
  description: string | null;
  createdBy: string | null;
  createdAt: string | null;
};
export type ShadowRow = {
  agentId: string;
  kind: string;
  action: string;
  channel: string | null;
  author: string | null;
  trigger: string | null;
  detail: string | null;
  createdAt: string | null;
  channelId: string | null;
  triggerMessageId: string | null;
};
export type AdviceRow = {
  id: number;
  agentId: string;
  text: string;
  streak: number;
  createdAt: string | null;
};
export type GlossaryEntry = {
  wrong: string;
  correct: string | null;
  createdBy: string | null;
  createdAt: string | null;
};
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
export type TaskRow = {
  key: string;
  kind: "action" | "homework" | "reminder";
  id: number;
  task: string | null;
  owner: string | null;
  due: string | null;
  status: string | null;
  stage: string | null;
};
export type LlmDaily = {
  day: string;
  calls: number;
  failed: number;
  costUsd: number;
  avgMs: number | null;
  maxMs: number | null;
  cacheReadTokens: number;
  outputTokens: number;
};
export type LlmPurpose = {
  agentId: string | null;
  purpose: string | null;
  calls: number;
  failed: number;
  costUsd: number;
  avgMs: number | null;
};
export type LlmCall = {
  id: number;
  agentId: string | null;
  purpose: string | null;
  model: string | null;
  ok: number | null;
  error: string | null;
  durationMs: number | null;
  numTurns: number | null;
  costUsd: number | null;
  toolCalls: number | null;
  denials: number | null;
  createdAt: string | null;
};
