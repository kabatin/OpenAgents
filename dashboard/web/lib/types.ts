/** サーバー（server/views.ts・server/config/types.ts）と対応する型。 */

export type SettingKind =
  | "bool"
  | "tri"
  | "int"
  | "string"
  | "text"
  | "enum"
  | "stringList"
  | "intList"
  | "hour"
  | "weekday"
  | "monthday"
  | "info";

export type EnumOption = { value: string | number | null; label: string };

export type ResolvedSetting = {
  path: string;
  label: string;
  desc: string;
  kind: SettingKind;
  default?: unknown;
  min?: number;
  max?: number;
  unit?: string;
  options?: EnumOption[];
  readonly?: boolean;
  secret?: boolean;
  fixedNote?: string;
  requires?: { path: string; label: string }[];
  /** "basic" = 初めての人がまず触る「よく使う設定」。無指定は詳細設定（既定では畳む） */
  level?: "basic";
  /** 自発ループの分類（画面で小見出しにまとめる） */
  category?: string;
  current: { path: string; value: unknown; explicit: boolean };
  blockedBy: string[];
  children?: ResolvedSetting[];
};

export type ResolvedGroup = {
  id: string;
  label: string;
  desc?: string;
  settings: ResolvedSetting[];
};

export type AgentSummary = {
  id: string;
  name: string;
  role: string;
  homeChannelId: string;
  service: "archivebot" | "devbot";
  proactiveEnabled: boolean;
  quota: { used: number; limit: number; source: string };
  lastRunAt: string | null;
  skillCount: number;
  cycleCount: { enabled: number; total: number };
  requireMention: boolean;
};

export type HealthStatus = "ok" | "down" | "disconnected" | "stalled" | "idle" | "unknown";

export type ServiceStatus = {
  id: string;
  label: string;
  /** 設定でオフにされている（異常ではない） */
  enabled: boolean;
  note?: string;
  pid: number | null;
  /** 再起動の回数。増え続けていればクラッシュループ */
  restarts: number;
  uptimeSec: number | null;
  lastExit: string | null;
  logAgeSec: number | null;
  logSizeBytes: number | null;
  heartbeatAgeSec: number | null;
  heartbeatDetail: string | null;
  status: HealthStatus;
  statusLabel: string;
  detail: string;
};

export type PendingView = {
  owner: "archivebot" | "devbot" | "meetingbot";
  label: string;
  count: number;
  changes: { path: string; before: unknown; after: unknown; label: string }[];
};

export type Overview = {
  agents: AgentSummary[];
  services: ServiceStatus[];
  pending: PendingView[];
  guildId: string | null;
};

export type AgentDetail = {
  id: string;
  name: string;
  service: "archivebot" | "devbot";
  summary: AgentSummary | null;
  groups: ResolvedGroup[];
  secrets: Record<string, string>;
  /** Discordの生ID → 表示名（`#チャンネル名` / ユーザー名）。引けたものだけ。 */
  idNames: Record<string, string>;
};

export type SettingsView = {
  agents: { id: string; name: string; requireMention: boolean }[];
  global: ResolvedGroup[];
  devBot: ResolvedGroup[];
  meetingBot: ResolvedGroup[];
  meetingUserMapping: Record<string, string>;
  secrets: Record<string, string>;
  idNames: Record<string, string>;
  monitorTargets: {
    name: string;
    launchdLabel: string;
    logPath: string;
    presenceBotNames: string[];
  }[];
};

export type ActivityRow = {
  id: number;
  createdAt: string;
  agentId: string;
  kind: string;
  action: string;
  channelId: string | null;
  channelName: string | null;
  detail: string | null;
  postedMessageId: string | null;
  triggerAuthor?: string | null;
  triggerExcerpt?: string | null;
  postedExcerpt?: string | null;
};

export type QuotaRow = { agentId: string; used: number; dbOverride: number | null };

export type LogLine = {
  seq: number;
  text: string;
  level: "error" | "warn" | "info" | "debug";
  agent: string | null;
  timestamp: string | null;
  boundary: boolean;
  noisy: boolean;
};

export type RestartResult = { ok: boolean; pid: number | null; detail: string; label: string };

export type ApiError = { message: string; issues?: { path: string; message: string }[] };
