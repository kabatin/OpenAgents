import { useState } from "react";

import { Card, Chip, Empty, Metric } from "../components/ui.tsx";
import { useFetch } from "../lib/api.ts";
import { agentLabel, jstStamp, since } from "../lib/format.ts";

type Summary = {
  counters: {
    activeRules: number;
    capabilityRequests: { status: string; count: number }[];
    roadmap: { status: string; count: number }[];
    feedback: { agentId: string; up: number; down: number }[];
    webhookAgents: { id: string; name: string; status: string; homeChannelId: number | null }[];
    sheetRegistry: { alias: string; title: string | null; mode: string; active: number }[];
    messages: number;
    channels: number;
  };
  reminders: { active: number; error: number; total: number };
};

type Rule = {
  id: number;
  agentId: string;
  scope: string;
  ruleText: string;
  createdBy: string | null;
  active: number;
  createdAt: string;
  expiresAt: string | null;
};

type Capability = {
  id: number;
  agentId: string;
  description: string;
  status: string;
  createdAt: string;
};

type Reminder = {
  id: number;
  user_name: string;
  content: string;
  due: string;
  repeat: string;
  status: string;
  channel_label?: string;
  agent_id: string;
};

const TABS = [
  { id: "rules", label: "ルール記憶" },
  { id: "reminders", label: "リマインダー" },
  { id: "capabilities", label: "能力リクエスト" },
  { id: "personas", label: "Webhook人格" },
  { id: "sheets", label: "シート登録簿" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** scope は `global` / `channel:<id>` / `user:<id>` の形で入っている。 */
function scopeLabel(scope: string): string {
  if (scope === "global") return "全体";
  if (scope.startsWith("channel:")) return "チャンネル限定";
  if (scope.startsWith("user:")) return "個人限定";
  return scope;
}

export function DataPage() {
  const [tab, setTab] = useState<TabId>("rules");
  const { data: summary } = useFetch<Summary>("/data/summary");
  const { data: rules } = useFetch<Rule[]>(tab === "rules" ? "/data/rules" : null);
  const { data: caps } = useFetch<Capability[]>(tab === "capabilities" ? "/data/capabilities" : null);
  const { data: reminders } = useFetch<{ items: Reminder[] }>(
    tab === "reminders" ? "/data/reminders" : null,
  );

  const c = summary?.counters;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">データ</h1>
        <p className="mt-1 max-w-[70ch] text-xs leading-relaxed text-muted">
          エージェントが実際に貯めているもの。ここは閲覧のみで、変更はDiscordでの会話から行います。
        </p>
      </div>

      <Card>
        <div className="grid grid-cols-2 gap-5 p-4 md:grid-cols-5">
          <Metric label="有効なルール" value={c?.activeRules ?? "—"} tone="accent" />
          <Metric
            label="動いているリマインダー"
            value={summary?.reminders.active ?? "—"}
            sub={summary?.reminders.error ? `${summary.reminders.error}件がエラー` : undefined}
            tone={summary?.reminders.error ? "danger" : "ink"}
          />
          <Metric
            label="未対応の能力リクエスト"
            value={c?.capabilityRequests.find((x) => x.status === "open")?.count ?? 0}
            sub={`全 ${c?.capabilityRequests.reduce((n, x) => n + x.count, 0) ?? 0} 件`}
          />
          <Metric label="蓄積メッセージ" value={(c?.messages ?? 0).toLocaleString()} sub={`${c?.channels ?? 0} ch`} />
        </div>
      </Card>

      {(c?.feedback.length ?? 0) > 0 && (
        <Card title="評価（👍👎）" desc="エージェントの投稿に付いたリアクション。物差しの原料です。">
          <div className="flex flex-wrap gap-6 p-4">
            {c?.feedback.map((f) => (
              <div key={f.agentId}>
                <div className="eyebrow">{agentLabel(f.agentId)}</div>
                <div className="tnum mt-1 flex items-baseline gap-3">
                  <span className="text-xl font-semibold text-accent-deep">👍 {f.up}</span>
                  <span className={`text-sm ${f.down > 0 ? "text-danger" : "text-faint"}`}>
                    👎 {f.down}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="flex flex-wrap gap-1 border-b border-hairline">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`focus-ring -mb-px border-b-2 px-3 py-2 text-[13px] transition-colors duration-100
              ${
                tab === t.id
                  ? "border-accent font-medium text-ink"
                  : "border-transparent text-muted hover:text-ink"
              }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <Card>
        {tab === "rules" && (
          <ul>
            {(rules ?? []).length === 0 ? (
              <Empty>ルールはありません</Empty>
            ) : (
              rules?.map((r) => (
                <li key={r.id} className="border-t border-hairline px-4 py-2.5 first:border-t-0">
                  <div className="flex items-baseline gap-2.5">
                    <span className="tnum w-8 shrink-0 text-2xs text-faint">#{r.id}</span>
                    <Chip tone={r.scope === "global" ? "info" : "neutral"}>
                      {scopeLabel(r.scope)}
                    </Chip>
                    <span className={`min-w-0 flex-1 text-xs ${r.active ? "" : "text-faint line-through"}`}>
                      {r.ruleText}
                    </span>
                    {r.expiresAt !== null && <Chip tone="warn">{r.expiresAt} まで</Chip>}
                    {r.active === 0 && <Chip>無効</Chip>}
                  </div>
                </li>
              ))
            )}
          </ul>
        )}

        {tab === "reminders" && (
          <ul>
            {(reminders?.items ?? []).filter((r) => r.status === "active").length === 0 ? (
              <Empty>動いているリマインダーはありません</Empty>
            ) : (
              reminders?.items
                .filter((r) => r.status === "active")
                .map((r) => (
                  <li
                    key={r.id}
                    className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
                  >
                    <span className="tnum w-8 shrink-0 text-2xs text-faint">#{r.id}</span>
                    <span className="tnum w-28 shrink-0 font-medium">{jstStamp(r.due)}</span>
                    {r.repeat !== "once" && <Chip tone="info">{r.repeat}</Chip>}
                    <span className="min-w-0 flex-1 truncate">{r.content}</span>
                    <span className="shrink-0 text-2xs text-faint">
                      {r.channel_label ?? ""} / {r.user_name}
                    </span>
                  </li>
                ))
            )}
          </ul>
        )}

        {tab === "capabilities" && (
          <ul>
            {(caps ?? []).length === 0 ? (
              <Empty>能力リクエストはありません</Empty>
            ) : (
              caps?.map((r) => (
                <li
                  key={r.id}
                  className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
                >
                  <span className="tnum w-8 shrink-0 text-2xs text-faint">#{r.id}</span>
                  <span className="w-14 shrink-0 text-2xs text-muted">{agentLabel(r.agentId)}</span>
                  <span className="min-w-0 flex-1">{r.description}</span>
                  <Chip
                    tone={
                      r.status === "deployed" ? "accent" : r.status === "rejected" ? "danger" : "warn"
                    }
                  >
                    {r.status === "deployed" ? "実装済み" : r.status === "rejected" ? "却下" : "未対応"}
                  </Chip>
                </li>
              ))
            )}
          </ul>
        )}

        {tab === "personas" && (
          <ul>
            {(c?.webhookAgents ?? []).length === 0 ? (
              <Empty>Webhook人格はいません</Empty>
            ) : (
              c?.webhookAgents.map((a) => (
                <li
                  key={a.id}
                  className="flex items-center gap-3 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
                >
                  <span className="font-medium">{a.name}</span>
                  <span className="font-mono text-2xs text-faint">{a.id}</span>
                  <span className="ml-auto">
                    <Chip tone={a.status === "active" ? "accent" : "neutral"}>
                      {a.status === "active" ? "稼働中" : "退役"}
                    </Chip>
                  </span>
                </li>
              ))
            )}
          </ul>
        )}

        {tab === "sheets" && (
          <ul>
            {(c?.sheetRegistry ?? []).length === 0 ? (
              <Empty>登録されたシートはありません</Empty>
            ) : (
              c?.sheetRegistry.map((s) => (
                <li
                  key={s.alias}
                  className="flex items-center gap-3 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
                >
                  <span className="font-medium">{s.alias}</span>
                  <span className="min-w-0 flex-1 truncate text-muted">{s.title ?? "—"}</span>
                  <Chip tone={s.mode === "rw" ? "warn" : "neutral"}>
                    {s.mode === "rw" ? "読み書き" : "読み取り"}
                  </Chip>
                  {s.active === 0 && <Chip>無効</Chip>}
                </li>
              ))
            )}
          </ul>
        )}

      </Card>
    </div>
  );
}
