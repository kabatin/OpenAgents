import { Link } from "react-router-dom";

import { Avatar } from "../components/Avatar.tsx";
import { Card, Chip, Empty, Metric, StatusDot } from "../components/ui.tsx";
import { useFetch } from "../lib/api.ts";
import {
  actionLabel,
  agentLabel,
  bytes,
  jstStamp,
  kindLabel,
  relTime,
  STATUS_TONE,
} from "../lib/format.ts";
import type { ActivityRow, AgentSummary, ServiceStatus } from "../lib/types.ts";

function QuotaBar({ used, limit }: { used: number; limit: number }) {
  const pct = limit === 0 ? 0 : Math.min(100, (used / limit) * 100);
  return (
    <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[#EDEBE7]">
      <div
        className="h-full rounded-full bg-accent transition-all duration-500"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function AgentCard({ agent, service }: { agent: AgentSummary; service?: ServiceStatus }) {
  const status = service?.status ?? "unknown";
  const tone = STATUS_TONE[status];
  return (
    <Link
      to={`/agents/${agent.id}`}
      className="focus-ring card group block p-4 transition-shadow duration-150 hover:shadow-pop"
    >
      <div className="flex items-start gap-3">
        <Avatar id={agent.id} name={agent.name} size="md" status={status} />
        <div className="min-w-0 flex-1">
          <span className="text-[15px] font-semibold tracking-tight">{agent.name}</span>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted">
            {agent.role === "" ? "全般担当" : agent.role}
          </p>
        </div>
        <span className={`chip shrink-0 ${tone.chip}`}>{service?.statusLabel ?? "不明"}</span>
      </div>

      {/* 開発BOTは観察ループを持たない別プロセスなので、意味のない 0/0 は出さない */}
      {agent.service === "devbot" ? (
        <div className="mt-4 grid grid-cols-3 gap-3">
          <div>
            <div className="eyebrow">役割</div>
            <div className="mt-1 text-sm font-medium leading-none">プロセス監視</div>
          </div>
          <div>
            <div className="eyebrow">プロセス</div>
            <div className="mt-1 text-sm font-medium leading-none">単独</div>
          </div>
          <div>
            <div className="eyebrow">生存証明</div>
            <div className="tnum mt-1 text-sm font-medium leading-none">
              {relTime(service?.heartbeatAgeSec ?? null)}
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-4 grid grid-cols-3 gap-3">
          <div>
            <div className="eyebrow">本日の枠</div>
            <div className="tnum mt-1 text-lg font-semibold leading-none">
              {agent.quota.used}
              <span className="text-xs font-normal text-faint">/{agent.quota.limit}</span>
            </div>
            <QuotaBar used={agent.quota.used} limit={agent.quota.limit} />
          </div>
          <div>
            <div className="eyebrow">自発ループ</div>
            <div className="tnum mt-1 text-lg font-semibold leading-none">
              {agent.cycleCount.enabled}
              <span className="text-xs font-normal text-faint">/{agent.cycleCount.total}</span>
            </div>
          </div>
          <div>
            <div className="eyebrow">最終観察</div>
            <div className="tnum mt-1 text-sm font-medium leading-none">
              {jstStamp(agent.lastRunAt)}
            </div>
          </div>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-1.5 border-t border-hairline pt-3">
        {agent.service === "devbot" ? (
          <>
            <Chip tone="info">承認ゲート型の自己改修</Chip>
            {agent.proactiveEnabled ? <Chip tone="accent">週次レポート ON</Chip> : <Chip>週次レポート OFF</Chip>}
          </>
        ) : (
          <>
            {agent.proactiveEnabled ? <Chip tone="accent">自発 ON</Chip> : <Chip>自発 OFF</Chip>}
            {agent.requireMention && <Chip>呼ばれた時だけ</Chip>}
            {agent.skillCount > 0 && <Chip tone="info">スキル {agent.skillCount}</Chip>}
            {agent.quota.source !== "config" && <Chip tone="plum">枠を会話で変更中</Chip>}
          </>
        )}
      </div>
    </Link>
  );
}

export function OverviewPage({
  agents,
  services,
  activity,
}: {
  agents: AgentSummary[];
  services: ServiceStatus[];
  activity: ActivityRow[];
}) {
  // 表示名は設定から来る（固定の対応表を持たない）
  const agentNames = Object.fromEntries(agents.map((a) => [a.id, a.name]));
  const { data: initialActivity } = useFetch<ActivityRow[]>("/activity?limit=40");
  const rows = [...activity, ...(initialActivity ?? [])]
    .filter((r, i, arr) => arr.findIndex((x) => x.id === r.id) === i)
    .sort((a, b) => b.id - a.id)
    .slice(0, 40);

  const serviceFor = (a: AgentSummary) =>
    services.find((s) => s.id === (a.service === "devbot" ? "devbot" : "archivebot"));

  const problems = services.filter(
    (s) => s.status === "down" || s.status === "disconnected" || s.status === "stalled",
  );
  const bigLogs = services.filter(
    (s) => (s.logSizeBytes ?? 0) > 40 * 1024 * 1024,
  );

  return (
    <div className="space-y-6">
      {problems.length > 0 && (
        <div className="rounded-lg border border-danger/25 bg-danger-soft px-4 py-3">
          <div className="text-xs font-semibold text-danger">要対応</div>
          <ul className="mt-1.5 space-y-1">
            {problems.map((p) => (
              <li key={p.id} className="text-xs text-danger">
                {p.label} — {p.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h1 className="text-[17px] font-semibold tracking-tight">エージェント</h1>
          <span className="text-2xs text-faint">
            会話エージェントは全員1つのプロセスで動いています（再起動は全員同時）
          </span>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {agents.map((a) => (
            <AgentCard key={a.id} agent={a} service={serviceFor(a)} />
          ))}
        </div>
      </section>

      {/* minmax(0,…) が無いと fr トラックの min-width:auto で長いログ行が列を押し広げる */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Card
          title="自発行動のタイムライン"
          desc="呼ばれていないのに自分から動いた記録（「黙った」判定は除外しています）"
          right={<span className="text-2xs text-faint">10秒ごとに更新</span>}
        >
          {rows.length === 0 ? (
            <Empty>まだ記録がありません</Empty>
          ) : (
            /* 内側にスクロール領域を作るとホイールが吸われて本文が最下部まで
               追えなくなるので、ページ側のスクロールに一本化する */
            <ul>
              {rows.map((r) => (
                <li
                  key={r.id}
                  className="flex items-baseline gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                >
                  <span className="tnum w-11 shrink-0 text-2xs text-faint">
                    {jstStamp(r.createdAt)}
                  </span>
                  <span className="flex w-[86px] shrink-0 items-center gap-1.5 self-center">
                    <Avatar id={r.agentId} name={agentLabel(r.agentId, agentNames)} size="sm" />
                    <span className="truncate font-medium">{agentLabel(r.agentId, agentNames)}</span>
                  </span>
                  <span className="shrink-0">
                    <Chip tone={r.action === "spoke" ? "accent" : "neutral"}>
                      {actionLabel(r.action)}
                    </Chip>
                  </span>
                  <span className="min-w-0 flex-1 truncate text-muted">
                    {kindLabel(r.kind)}
                    {r.channelName !== null && (
                      <span className="text-faint"> · #{r.channelName}</span>
                    )}
                    {r.detail !== null && r.detail !== "" && (
                      <span className="text-faint"> — {r.detail}</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-6">
          <Card title="プロセス">
            <ul>
              {services.map((s) => (
                <li
                  key={s.id}
                  className="flex items-center gap-3 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
                >
                  <StatusDot status={s.status} />
                  <span className="min-w-0 flex-1 truncate">{s.label}</span>
                  <span className="tnum shrink-0 text-2xs text-faint">
                    {s.pid === null ? "—" : `pid ${s.pid}`}
                  </span>
                  <span className={`chip shrink-0 ${STATUS_TONE[s.status].chip}`}>
                    {s.statusLabel}
                  </span>
                </li>
              ))}
            </ul>
          </Card>

          {bigLogs.length > 0 && (
            <Card title="ログの肥大" desc="50MBを超えると毎朝4時に1世代だけ退避されます">
              <ul>
                {bigLogs.map((s) => (
                  <li
                    key={s.id}
                    className="flex items-center justify-between gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                  >
                    <span className="truncate">{s.label}</span>
                    <span className="tnum shrink-0 text-warn">
                      {bytes(s.logSizeBytes)}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card title="今日の合計">
            <div className="grid grid-cols-2 gap-4 p-4">
              <Metric
                label="自発発言"
                value={agents.reduce((n, a) => n + a.quota.used, 0)}
                sub={`上限 ${agents.reduce((n, a) => n + a.quota.limit, 0)}`}
                tone="accent"
              />
              <Metric
                label="有効な自発ループ"
                value={agents.reduce((n, a) => n + a.cycleCount.enabled, 0)}
                sub={`全 ${agents.reduce((n, a) => n + a.cycleCount.total, 0)} 件中`}
              />
              <Metric
                label="会話エージェント稼働"
                value={relTime(
                  services.find((s) => s.id === "archivebot")?.uptimeSec ?? null,
                ).replace("前", "")}
                sub="前回の再起動から"
              />
              <Metric
                label="開発BOTの生存証明"
                value={relTime(services.find((s) => s.id === "devbot")?.heartbeatAgeSec ?? null)}
                sub="300秒を超えると自動再起動"
                tone={
                  (services.find((s) => s.id === "devbot")?.heartbeatAgeSec ?? 0) > 300
                    ? "danger"
                    : "ink"
                }
              />
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
