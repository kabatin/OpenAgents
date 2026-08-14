/**
 * APIが返すビューモデルの組み立て。
 *
 * 原則: **秘密情報の実値はここから先へ出さない。** token / api_key は
 * maskSecret() の結果だけを載せる（ルート側でうっかり生値を混ぜないよう、
 * 生のconfigをそのまま返すエンドポイントは作らない）。
 */
import {
  agentGroups,
  DEV_BOT_GROUPS,
  flatten,
  GLOBAL_GROUPS,
  maskSecret,
  MEETING_BOT_GROUPS,
  resolveGroups,
  type ResolvedGroup,
} from "./config/catalog.ts";
import { displayValue } from "./config/bigjson.ts";
import { getPath, type Json } from "./config/objpath.ts";
import { readConfig, readMeetingConfig } from "./config/store.ts";
import { CONFIG_OWNERS, ownerLabel, pendingFor, type ConfigOwner } from "./config/apply.ts";
import { configMtimeMs } from "./config/store.ts";
import { lastRuns, todayQuota } from "./db/queries.ts";
import { allServiceStatus, type ServiceStatus } from "./ops/status.ts";

export type AgentSummary = {
  id: string;
  name: string;
  role: string;
  homeChannelId: string;
  service: "archivebot" | "devbot";
  proactiveEnabled: boolean;
  quota: { used: number; limit: number; source: "config" | "会話で変更" };
  lastRunAt: string | null;
  skillCount: number;
  cycleCount: { enabled: number; total: number };
  requireMention: boolean;
};

type AgentRecord = Record<string, Json>;

export function agentsOf(config: Record<string, Json>): AgentRecord[] {
  const arr = config["agents"];
  return Array.isArray(arr) ? (arr as AgentRecord[]) : [];
}

export function agentIndex(config: Record<string, Json>, id: string): number {
  return agentsOf(config).findIndex((a) => a["id"] === id);
}

/** 自発サイクルの有効件数（画面の「33件中 28件有効」の分子・分母） */
function cycleCounts(agent: AgentRecord): { enabled: number; total: number } {
  const cycles = flatten(agentGroups()).filter(
    (s) => s.path.startsWith("proactive.") && s.path !== "proactive.enabled",
  );
  // グループ「自発ループ」のトップレベル行だけを数える（子パラメータは数えない）
  const tops = agentGroups()
    .filter((g) => g.id === "proactive-cycles")
    .flatMap((g) => g.settings);
  let enabled = 0;
  for (const s of tops) {
    const raw = getPath(agent, s.path);
    if (s.kind === "tri") {
      if ((raw as { enabled?: boolean } | undefined)?.enabled) enabled += 1;
    } else if (s.kind === "string") {
      if (typeof raw === "string" && raw.length > 0) enabled += 1;
    } else if (raw === undefined ? s.default === true : Boolean(raw)) {
      enabled += 1;
    }
  }
  void cycles;
  return { enabled, total: tops.length };
}

function skillCount(agent: AgentRecord): number {
  const skills = (agent["skills"] ?? {}) as Record<string, unknown>;
  return Object.values(skills).filter((v) => (typeof v === "object" ? v !== null : Boolean(v)))
    .length;
}

export async function agentSummaries(config: Record<string, Json>): Promise<AgentSummary[]> {
  const agents = agentsOf(config);
  const ids = agents.map((a) => String(a["id"]));
  const quotas = todayQuota(ids);
  const runs = lastRuns(ids);
  const quotaMap = new Map(quotas.map((q) => [q.agentId, q]));
  const runMap = new Map(runs.map((r) => [r.agentId, r.lastRunAt]));

  const summaries: AgentSummary[] = agents.map((a) => {
    const id = String(a["id"]);
    const pro = (a["proactive"] ?? {}) as Record<string, unknown>;
    const q = quotaMap.get(id);
    const configLimit = Number(pro["daily_quota"] ?? 3);
    const limit = q?.dbOverride ?? configLimit;
    return {
      id,
      name: String(a["name"] ?? id),
      role: String(a["role"] ?? ""),
      homeChannelId: String(a["home_channel_id"] ?? ""),
      service: "archivebot",
      proactiveEnabled: Boolean(pro["enabled"]),
      quota: {
        used: q?.used ?? 0,
        limit,
        source: q?.dbOverride == null ? "config" : "会話で変更",
      },
      lastRunAt: runMap.get(id) ?? null,
      skillCount: skillCount(a),
      cycleCount: cycleCounts(a),
      requireMention: Boolean(a["require_mention"]),
    };
  });

  // 開発BOTは agents[] に居ない（dev_bot セクションの別プロセス）ので手で足す。
  // ただし**有効なときだけ**。既定オフの機能を一覧に出すと、
  // 「居るのに動いていないBOT」に見えて混乱する
  const dev = (config["dev_bot"] ?? {}) as Record<string, unknown>;
  if (dev["enabled"] === true) {
  summaries.push({
    id: "devbot",
    name: "開発BOT",
    role: "開発BOT。他のBOTを監視し、承認つきで自分たちのコードを直す。",
    homeChannelId: String(dev["dev_channel_id"] ?? ""),
    service: "devbot",
    proactiveEnabled: Boolean(
      (dev["weekly_report"] as Record<string, unknown> | undefined)?.["enabled"],
    ),
    quota: { used: 0, limit: 0, source: "config" },
    lastRunAt: null,
    skillCount: 0,
    cycleCount: { enabled: 0, total: 0 },
    requireMention: false,
  });
  }

  return summaries;
}

export type AgentDetail = {
  id: string;
  name: string;
  service: "archivebot" | "devbot";
  summary: AgentSummary | null;
  groups: ResolvedGroup[];
  secrets: Record<string, string>;
};

export async function agentDetail(id: string): Promise<AgentDetail | null> {
  const config = await readConfig();
  if (id === "devbot") {
    const groups = resolveGroups(DEV_BOT_GROUPS, config, config);
    const summaries = await agentSummaries(config);
    return {
      id,
      name: "開発BOT",
      service: "devbot",
      summary: summaries.find((s) => s.id === "devbot") ?? null,
      groups,
      secrets: {
        "dev_bot.token": maskSecret(getPath(config, "dev_bot.token")),
      },
    };
  }
  const idx = agentIndex(config, id);
  if (idx < 0) return null;
  const agent = agentsOf(config)[idx] as AgentRecord;
  const summaries = await agentSummaries(config);
  return {
    id,
    name: String(agent["name"] ?? id),
    service: "archivebot",
    summary: summaries.find((s) => s.id === id) ?? null,
    groups: resolveGroups(agentGroups(), agent, config),
    secrets: { token: maskSecret(agent["token"]) },
  };
}

export type SettingsView = {
  agents: { id: string; name: string; requireMention: boolean }[];
  global: ResolvedGroup[];
  devBot: ResolvedGroup[];
  meetingBot: ResolvedGroup[];
  meetingUserMapping: Record<string, string>;
  secrets: Record<string, string>;
  monitorTargets: {
    name: string;
    launchdLabel: string;
    logPath: string;
    presenceBotNames: string[];
  }[];
};

export async function settingsView(): Promise<SettingsView> {
  const config = await readConfig();
  // エージェントの増減を画面から行うための一覧（トークンは載せない）
  const roster = ((getPath(config, "agents") ?? []) as Record<string, Json>[]).map((a) => ({
    id: String(a["id"] ?? ""),
    name: String(a["name"] ?? ""),
    requireMention: a["require_mention"] === true,
  }));
  let meeting: Record<string, Json> = {};
  try {
    meeting = (await readMeetingConfig()) as Record<string, Json>;
  } catch {
    meeting = {};
  }
  const targetsRaw = getPath(config, "dev_bot.monitor.targets");
  const monitorTargets = Array.isArray(targetsRaw)
    ? (targetsRaw as Record<string, Json>[]).map((t) => ({
        name: String(t["name"] ?? ""),
        launchdLabel: String(t["launchd_label"] ?? ""),
        logPath: String(t["log_path"] ?? ""),
        presenceBotNames: Array.isArray(t["presence_bot_names"])
          ? (t["presence_bot_names"] as string[])
          : [],
      }))
    : [];

  return {
    agents: roster,
    global: resolveGroups(GLOBAL_GROUPS, config, config),
    devBot: resolveGroups(DEV_BOT_GROUPS, config, config),
    meetingBot: resolveGroups(MEETING_BOT_GROUPS, meeting, meeting),
    meetingUserMapping: (meeting["user_mapping"] ?? {}) as Record<string, string>,
    secrets: {
      "dev_bot.token": maskSecret(getPath(config, "dev_bot.token")),
    },
    monitorTargets,
  };
}

export type PendingView = {
  owner: ConfigOwner;
  label: string;
  count: number;
  changes: { path: string; before: unknown; after: unknown; label: string }[];
};

/** カタログを引いて差分パスに日本語の見出しを付ける（生のJSONパスだけだと読めない）。 */
function labelForPath(configPath: string): string {
  const all = [
    ...flatten(GLOBAL_GROUPS),
    ...flatten(DEV_BOT_GROUPS),
    ...flatten(MEETING_BOT_GROUPS),
  ];
  const direct = all.find((s) => s.path === configPath);
  if (direct) return direct.label;

  const agentMatch = /^agents\.(\d+)\.(.+)$/.exec(configPath);
  if (agentMatch?.[2]) {
    const rest = agentMatch[2];
    const agentSetting = flatten(agentGroups()).find(
      (s) => s.path === rest || rest.startsWith(`${s.path}.`),
    );
    if (agentSetting) return agentSetting.label;
  }
  return configPath;
}

/** 差分に秘密情報が現れたら値を伏せる（外部からトークンが差し替えられた場合など）。 */
const SECRET_PATH_RE = /(^|\.)(token|api_key)$/;

function redactDiffValue(configPath: string, value: unknown): unknown {
  if (SECRET_PATH_RE.test(configPath)) return maskSecret(value);
  // 大きな整数の内部表現（目印つき文字列）を画面に出さない
  return displayValue(value);
}

export async function pendingChanges(services: ServiceStatus[]): Promise<PendingView[]> {
  const config = await readConfig();
  const mtime = await configMtimeMs();
  const out: PendingView[] = [];
  for (const owner of CONFIG_OWNERS) {
    const svc = services.find((s) => s.id === owner);
    // 無効なBOTの「再起動して適用」は出さない（押しても何も起きず、
    // 設定し忘れているように見える）
    if (svc !== undefined && !svc.enabled) continue;
    // 「設定を変えたが、そのBOTはまだ古い設定で動いている」を検出するため、
    // プロセスの起動時刻が要る。稼働時間から逆算する（稼働中でなければ null）
    const startedAtMs =
      svc?.uptimeSec === null || svc?.uptimeSec === undefined
        ? null
        : Date.now() - svc.uptimeSec * 1000;
    const { changes, unknownStale } = await pendingFor(owner, config, mtime, startedAtMs);
    const rows = changes.map((c) => ({
      path: c.path,
      before: redactDiffValue(c.path, c.before),
      after: redactDiffValue(c.path, c.after),
      label: labelForPath(c.path),
    }));
    if (unknownStale) {
      rows.push({
        path: "(不明)",
        before: null,
        after: null,
        label:
          "この画面を使う前に設定ファイルが直接編集されています（変更内容は追跡できません）",
      });
    }
    const names = ((getPath(config, "agents") ?? []) as { name?: string }[])
      .map((a) => String(a.name ?? ""))
      .filter(Boolean);
    out.push({ owner, label: ownerLabel(owner, names), count: rows.length, changes: rows });
  }
  return out;
}

export type Overview = {
  agents: AgentSummary[];
  services: ServiceStatus[];
  pending: PendingView[];
};

export async function overview(stallAfterSec: number): Promise<Overview> {
  const config = await readConfig();
  const services = await allServiceStatus(stallAfterSec);
  return {
    agents: await agentSummaries(config),
    services,
    pending: await pendingChanges(services),
  };
}

/** dev_bot.monitor.stall_after_sec を判定に使う（画面とBOTで閾値を揃える） */
export async function stallAfterSec(): Promise<number> {
  const config = await readConfig();
  const v = getPath(config, "dev_bot.monitor.stall_after_sec");
  return typeof v === "number" ? v : 1800;
}
