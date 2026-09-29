import { useState, type ReactNode } from "react";

import { Card, Chip, Empty } from "../components/ui.tsx";
import { useFetch } from "../lib/api.ts";
import { jstStamp } from "../lib/format.ts";
import type { SettingsView } from "../lib/types.ts";

type RoadmapRow = {
  id: number;
  title: string;
  category: string | null;
  route: string | null;
  status: string;
  createdAt: string;
};

type DevJobsView = {
  jobs: {
    id: number;
    capReqId: number | null;
    branch: string | null;
    status: string;
    summary: string | null;
    updatedAt: string;
  }[];
  deploys: {
    jobId: number;
    files: string | null;
    deployedAt: string;
    revertedAt: string | null;
    canaryStatus: string | null;
  }[];
};

const STATUS_TONE: Record<
  string,
  "accent" | "warn" | "info" | "neutral" | "danger"
> = {
  done: "accent",
  deployed: "accent",
  approved: "info",
  proposed: "info",
  pending: "neutral",
  skipped: "neutral",
  held: "warn",
  rejected: "danger",
  failed: "danger",
};

const STATUS_JA: Record<string, string> = {
  done: "完了",
  pending: "未着手",
  approved: "承認済み",
  proposed: "提案中（👍待ち）",
  queued_session: "セッション行き",
  built: "反映待ち（👍待ち）",
  interrupted: "中断",
  superseded: "作り直し済み",
  skipped: "見送り",
  held: "保留（返事なし）",
  deployed: "デプロイ済み",
  rejected: "却下",
  failed: "失敗",
  open: "未対応",
};

function ja(status: string): string {
  return STATUS_JA[status] ?? status;
}

const JOBS_SHOWN = 5;
const DEPLOYS_SHOWN = 5;

/** いま動いている（判断や作業を待っている）状態。完了・見送りは既定で隠す。 */
const ACTIVE_STATUSES = new Set([
  "pending",
  "proposed",
  "approved",
  "queued_session",
  "held",
]);

/** Discord 向けの装飾（**太字**・先頭の絵文字）を落として一覧で読める形にする。 */
function plainSummary(text: string | null): string {
  return (text ?? "")
    .replace(/\*\*/g, "")
    .replace(/^[^\p{L}\p{N}#]+/u, "")
    .trim();
}

/** 開発BOTのページにだけ出す固有パネル（監視対象・起票ロードマップ・開発ジョブ）。 */
export function DevBotPanels() {
  const { data: settings } = useFetch<SettingsView>("/settings");
  const { data: roadmap } = useFetch<RoadmapRow[]>("/data/roadmap");
  const { data: dev } = useFetch<DevJobsView>("/data/dev-jobs");
  const [statusFilter, setStatusFilter] = useState<string | null>(null);

  const byStatus = new Map<string, number>();
  for (const r of roadmap ?? [])
    byStatus.set(r.status, (byStatus.get(r.status) ?? 0) + 1);
  const shownRoadmap = (roadmap ?? []).filter((r) =>
    statusFilter === null
      ? ACTIVE_STATUSES.has(r.status)
      : r.status === statusFilter,
  );
  const waiting = [
    ...(roadmap ?? [])
      .filter((r) => r.status === "proposed")
      .map((r) => `ロードマップ#${r.id} ${r.title}`),
    ...(dev?.jobs ?? [])
      .filter((j) => j.status === "built")
      .map((j) => `起票#${j.capReqId ?? "?"} の反映`),
  ];

  return (
    <div className="space-y-6">
      <Card
        title="監視しているプロセス"
        desc="Discord上でオフライン表示になったら異常とみなします（人間の見え方と一致させるため）"
      >
        {(settings?.monitorTargets.length ?? 0) === 0 ? (
          <Empty>監視対象が設定されていません</Empty>
        ) : (
          <ul className="flex flex-wrap">
            {settings?.monitorTargets.map((t) => (
              <li
                key={t.name}
                className="min-w-[260px] flex-1 border-hairline px-4 py-3 [&:not(:first-child)]:border-l"
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{t.name}</span>
                  <span className="truncate font-mono text-2xs text-faint">
                    {t.launchdLabel}
                  </span>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  {t.presenceBotNames.length === 0 ? (
                    <Chip tone="warn">オンライン判定なし</Chip>
                  ) : (
                    t.presenceBotNames.map((n) => (
                      <Chip key={n}>{n} で判定</Chip>
                    ))
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {waiting.length > 0 && (
        <div className="rounded-lg border border-warn/25 bg-warn-soft px-4 py-3 text-xs">
          <div className="font-semibold text-warn">
            👍待ち（AI開発室で判断が要るもの）
          </div>
          <ul className="mt-1 list-disc pl-4 text-ink">
            {waiting.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <p className="mt-1 text-2xs text-muted">
            7日お返事がないと保留にして次へ進みます（全体設定の「👍待ちの期限」）
          </p>
        </div>
      )}

      <Card
        title="起票ロードマップ"
        desc="能力リクエストから生まれた開発項目。開発BOTが実装するものと、人間のセッションに渡すものがあります。"
        right={<span className="tnum text-2xs text-faint">全 {roadmap?.length ?? 0} 件</span>}
      >
        <div
          className="flex flex-wrap gap-1.5 border-b border-hairline px-4 py-3"
          role="group"
          aria-label="状態で絞り込む"
        >
          <FilterChip
            active={statusFilter === null}
            onClick={() => setStatusFilter(null)}
          >
            進行中のもの
          </FilterChip>
          {[...byStatus.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([status, n]) => (
              <FilterChip
                key={status}
                active={statusFilter === status}
                onClick={() =>
                  setStatusFilter(statusFilter === status ? null : status)
                }
              >
                {ja(status)} {n}
              </FilterChip>
            ))}
        </div>
        {shownRoadmap.length === 0 ? (
          <Empty>
            {statusFilter === null
              ? "いま進行中の項目はありません"
              : "該当する項目はありません"}
          </Empty>
        ) : (
          <ul>
            {shownRoadmap.map((r) => (
              <li
                key={r.id}
                className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
              >
                <span className="tnum w-9 shrink-0 text-2xs text-faint">
                  #{r.id}
                </span>
                <span className="min-w-0 flex-1 truncate">{r.title}</span>
                {r.route !== null && (
                  <span className="shrink-0 text-2xs text-faint">
                    {r.route}
                  </span>
                )}
                <Chip tone={STATUS_TONE[r.status] ?? "neutral"}>
                  {ja(r.status)}
                </Chip>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="開発ジョブ"
          desc="開発BOTが作業場（worktree）で実装 → 👍で承認 → 本番に反映、の記録"
        >
          {(dev?.jobs.length ?? 0) === 0 ? (
            <Empty>ジョブはありません</Empty>
          ) : (
            <ul>
              {dev?.jobs.slice(0, JOBS_SHOWN).map((j) => (
                <li
                  key={j.id}
                  className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                >
                  <span className="tnum w-9 shrink-0 text-2xs text-faint">
                    #{j.id}
                  </span>
                  <span
                    className="min-w-0 flex-1 truncate"
                    title={plainSummary(j.summary)}
                  >
                    {plainSummary(j.summary) || j.branch || "（要約なし）"}
                  </span>
                  <span className="tnum shrink-0 text-2xs text-faint">
                    {jstStamp(j.updatedAt)}
                  </span>
                  <Chip tone={STATUS_TONE[j.status] ?? "neutral"}>
                    {ja(j.status)}
                  </Chip>
                </li>
              ))}
              {(dev?.jobs.length ?? 0) > JOBS_SHOWN && (
                <li className="border-t border-hairline px-4 py-2 text-2xs text-muted">
                  他 {(dev?.jobs.length ?? 0) - JOBS_SHOWN} 件（新しい順に
                  {JOBS_SHOWN}件だけ表示しています）
                </li>
              )}
            </ul>
          )}
        </Card>

        <Card
          title="本番への反映履歴"
          desc="反映後24時間はエラーログの急増を見張ります（カナリア）"
        >
          {(dev?.deploys.length ?? 0) === 0 ? (
            <Empty>反映はまだありません</Empty>
          ) : (
            <ul>
              {dev?.deploys.slice(0, DEPLOYS_SHOWN).map((d) => (
                <li
                  key={`${d.jobId}-${d.deployedAt}`}
                  className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                >
                  <span className="tnum w-9 shrink-0 text-2xs text-faint">
                    #{d.jobId}
                  </span>
                  <span
                    className="min-w-0 flex-1 truncate text-muted"
                    title={d.files ?? ""}
                  >
                    {d.files ?? "—"}
                  </span>
                  <span className="tnum shrink-0 text-2xs text-faint">
                    {jstStamp(d.deployedAt)}
                  </span>
                  {d.revertedAt !== null ? (
                    <Chip tone="danger">巻き戻し済み</Chip>
                  ) : (
                    <Chip tone={d.canaryStatus === "alert" ? "warn" : "accent"}>
                      {d.canaryStatus === "alert" ? "カナリア警告" : "稼働中"}
                    </Chip>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`focus-ring rounded-full border px-2.5 py-0.5 text-2xs font-medium transition-colors ${
        active
          ? "border-accent bg-accent text-white"
          : "border-hairline bg-surface text-muted hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}
