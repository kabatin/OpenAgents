import { useState } from "react";
import { Link } from "react-router-dom";

import { Avatar } from "../components/Avatar.tsx";
import {
  Card,
  Chip,
  Empty,
  ErrorNote,
  Loading,
  Metric,
  StatusDot,
} from "../components/ui.tsx";
import { useFetch } from "../lib/api.ts";
import {
  actionLabel,
  agentLabel,
  bytes,
  discordUrl,
  plainDiscord,
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

function AgentCard({
  agent,
  service,
}: {
  agent: AgentSummary;
  service?: ServiceStatus;
}) {
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
          <span className="text-[15px] font-semibold tracking-tight">
            {agent.name}
          </span>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted">
            {agent.role === "" ? "全般担当" : agent.role}
          </p>
        </div>
        <span className={`chip shrink-0 ${tone.chip}`}>
          {service?.statusLabel ?? "不明"}
        </span>
      </div>

      {/* 開発BOTは観察ループを持たない別プロセスなので、意味のない 0/0 は出さない */}
      {agent.service === "devbot" ? (
        <div className="mt-4 grid grid-cols-3 gap-3">
          <div>
            <div className="eyebrow">役割</div>
            <div className="mt-1 text-sm font-medium leading-none">
              プロセス監視
            </div>
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
              <span className="text-xs font-normal text-faint">
                /{agent.quota.limit}
              </span>
            </div>
            <QuotaBar used={agent.quota.used} limit={agent.quota.limit} />
          </div>
          <div>
            <div className="eyebrow">自発ループ</div>
            <div className="tnum mt-1 text-lg font-semibold leading-none">
              {agent.cycleCount.enabled}
              <span className="text-xs font-normal text-faint">
                /{agent.cycleCount.total}
              </span>
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
            {agent.proactiveEnabled ? (
              <Chip tone="accent">週次レポート ON</Chip>
            ) : (
              <Chip>週次レポート OFF</Chip>
            )}
          </>
        ) : (
          <>
            {agent.proactiveEnabled ? (
              <Chip tone="accent">自発 ON</Chip>
            ) : (
              <Chip>自発 OFF</Chip>
            )}
            {agent.requireMention && <Chip>呼ばれた時だけ</Chip>}
            {agent.skillCount > 0 && (
              <Chip tone="info">スキル {agent.skillCount}</Chip>
            )}
            {agent.quota.source !== "config" && (
              <Chip tone="plum">枠を会話で変更中</Chip>
            )}
          </>
        )}
      </div>
    </Link>
  );
}

const TIMELINE_FIRST = 15;

function dayOf(raw: string): string {
  return raw.slice(0, 10);
}

/** 日付の区切り見出し（今日／昨日／M月D日）。 */
function dayLabel(raw: string): string {
  const today = new Date(Date.now() + 9 * 3600 * 1000)
    .toISOString()
    .slice(0, 10);
  const yesterday = new Date(Date.now() + 9 * 3600 * 1000 - 86_400_000)
    .toISOString()
    .slice(0, 10);
  const d = dayOf(raw);
  if (d === today) return "今日";
  if (d === yesterday) return "昨日";
  const [, m, day] = d.split("-");
  return `${Number(m)}月${Number(day)}日`;
}

/**
 * タイムラインの1件。畳んだ状態でも「誰が・どこで・何をしたか・中身の冒頭」が分かり、
 * 押すと全文（記録の詳細・きっかけの発言・投稿した本文）とDiscordへのリンクが出る。
 */
function ActivityItem({
  row: r,
  guildId,
  dayHeader,
  names,
}: {
  row: ActivityRow;
  guildId: string | null;
  dayHeader: string | null;
  /** エージェントIDから表示名へ（設定由来。固定の対応表は持たない） */
  names: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const url = discordUrl(guildId, r.channelId, r.postedMessageId);
  const detail = plainDiscord(r.detail);
  const posted = plainDiscord(r.postedExcerpt);
  const trigger = plainDiscord(r.triggerExcerpt);
  const preview = posted || detail;
  const tone =
    r.action === "spoke" || r.action.endsWith("offered")
      ? "accent"
      : r.action.includes("shadow")
        ? "neutral"
        : "info";
  return (
    <>
      {dayHeader !== null && (
        <li className="border-t border-hairline bg-canvas px-4 py-1 text-2xs font-semibold text-muted first:border-t-0">
          {dayHeader}
        </li>
      )}
      <li className="border-t border-hairline">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="focus-ring flex w-full items-start gap-3 px-4 py-2.5 text-left text-xs hover:bg-canvas/60"
        >
          <span className="tnum mt-0.5 w-10 shrink-0 text-2xs text-faint">
            {r.createdAt.slice(11, 16)}
          </span>
          <Avatar id={r.agentId} name={agentLabel(r.agentId, names)} size="sm" />
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
              <span className="font-medium">{agentLabel(r.agentId, names)}</span>
              {r.channelName !== null && (
                <span className="text-muted">が #{r.channelName} で</span>
              )}
              {r.channelName === null && <span className="text-muted">が</span>}
              <span className="text-muted">{kindLabel(r.kind)}：</span>
              <Chip tone={tone}>{actionLabel(r.action)}</Chip>
            </span>
            {preview !== "" && (
              <span
                className={`mt-1 text-muted ${open ? "block whitespace-pre-wrap" : "line-clamp-2"}`}
              >
                {preview}
              </span>
            )}
          </span>
          <span
            className={`mt-0.5 shrink-0 text-2xs text-faint transition-transform ${open ? "rotate-90" : ""}`}
          >
            ▸
          </span>
        </button>
        {open && (
          <div className="space-y-2 px-4 pb-3 pl-[76px] text-xs">
            {trigger !== "" && (
              <div>
                <div className="eyebrow">きっかけ</div>
                <p className="mt-0.5 whitespace-pre-wrap text-muted">
                  {r.triggerAuthor ?? "誰か"}「{trigger}」
                </p>
              </div>
            )}
            {detail !== "" && detail !== posted && (
              <div>
                <div className="eyebrow">記録</div>
                <p className="mt-0.5 whitespace-pre-wrap text-muted">
                  {detail}
                </p>
              </div>
            )}
            {url !== null && (
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                className="focus-ring inline-block rounded text-2xs text-accent-deep underline decoration-dotted"
              >
                Discordで投稿を開く
              </a>
            )}
          </div>
        )}
      </li>
    </>
  );
}

export function OverviewPage({
  agents,
  services,
  activity,
  guildId,
  loading,
  error,
  onRetry,
  connected,
}: {
  agents: AgentSummary[];
  services: ServiceStatus[];
  activity: ActivityRow[];
  guildId: string | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  connected: boolean;
}) {
  // 表示名は設定から来る（固定の対応表を持たない）
  const agentNames = Object.fromEntries(agents.map((a) => [a.id, a.name]));
  const activityQ = useFetch<ActivityRow[]>("/activity?limit=40");
  const initialActivity = activityQ.data;
  const rows = [...activity, ...(initialActivity ?? [])]
    .filter((r, i, arr) => arr.findIndex((x) => x.id === r.id) === i)
    .sort((a, b) => b.id - a.id)
    .slice(0, 40);
  const [showAll, setShowAll] = useState(false);
  const shownRows = showAll ? rows : rows.slice(0, TIMELINE_FIRST);

  const serviceFor = (a: AgentSummary) =>
    services.find(
      (s) => s.id === (a.service === "devbot" ? "devbot" : "archivebot"),
    );

  const problems = services.filter(
    (s) =>
      s.status === "down" ||
      s.status === "disconnected" ||
      s.status === "stalled",
  );
  const bigLogs = services.filter(
    (s) => (s.logSizeBytes ?? 0) > 40 * 1024 * 1024,
  );

  if (error !== null) {
    return (
      <div className="space-y-4">
        <ErrorNote
          message={`全体の状況を取得できませんでした: ${error}`}
          onRetry={onRetry}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* SSEが切れている間は「今の状態」を保証できない。黙って古い値を出さない */}
      {!connected && !loading && (
        <div className="rounded-lg border border-warn/25 bg-warn-soft px-4 py-2.5 text-xs text-warn">
          サーバーとの接続が切れています。下の状態は最後に受け取った時点のもので、今の状態とは違う可能性があります。
        </div>
      )}

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
          <h1 className="text-[17px] font-semibold tracking-tight">
            エージェント
          </h1>
          <span className="text-2xs text-faint">
            会話エージェントは全員1つのプロセスで動いています（再起動は全員同時）
          </span>
        </div>
        {loading && agents.length === 0 ? (
          <div className="card">
            <Loading rows={4} />
          </div>
        ) : agents.length === 0 ? (
          <Card>
            <Empty>エージェントが登録されていません</Empty>
          </Card>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {agents.map((a) => (
              <AgentCard key={a.id} agent={a} service={serviceFor(a)} />
            ))}
          </div>
        )}
      </section>

      {/* minmax(0,…) が無いと fr トラックの min-width:auto で長いログ行が列を押し広げる */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Card
          title="自発行動のタイムライン"
          desc="呼ばれていないのに自分から動いた記録です（「黙った」判定は除外）。行を押すと、きっかけの発言と投稿した内容が見られます"
          right={<span className="text-2xs text-faint">10秒ごとに更新</span>}
        >
          {rows.length === 0 ? (
            <Empty>まだ記録がありません</Empty>
          ) : (
            /* 内側にスクロール領域を作るとホイールが吸われて本文が最下部まで
               追えなくなるので、ページ側のスクロールに一本化する */
            <ul>
              {shownRows.map((r, i) => (
                <ActivityItem
                  key={r.id}
                  row={r}
                  guildId={guildId}
                  names={agentNames}
                  dayHeader={
                    i === 0 ||
                    dayOf(rows[i - 1]!.createdAt) !== dayOf(r.createdAt)
                      ? dayLabel(r.createdAt)
                      : null
                  }
                />
              ))}
              {rows.length > TIMELINE_FIRST && (
                <li className="border-t border-hairline">
                  <button
                    type="button"
                    onClick={() => setShowAll((v) => !v)}
                    className="focus-ring w-full px-4 py-2 text-center text-2xs text-muted hover:text-ink"
                  >
                    {showAll
                      ? "最近のものだけにする"
                      : `もっと見る（あと${rows.length - TIMELINE_FIRST}件）`}
                  </button>
                </li>
              )}
            </ul>
          )}
        </Card>

        <div className="space-y-6">
          <Card title="プロセス">
            {loading && services.length === 0 && <Loading rows={3} />}
            {!loading && services.length === 0 && (
              <Empty>プロセスの情報を取得できていません</Empty>
            )}
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
                  <span
                    className={`chip shrink-0 ${STATUS_TONE[s.status].chip}`}
                  >
                    {s.statusLabel}
                  </span>
                </li>
              ))}
            </ul>
          </Card>

          {bigLogs.length > 0 && (
            <Card
              title="ログの肥大"
              desc="50MBを超えると毎朝4時に1世代だけ退避されます"
            >
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
                  (services.find((s) => s.id === "devbot")?.heartbeatAgeSec ??
                    0) > 300
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
