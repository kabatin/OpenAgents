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
  jobs: { id: number; capReqId: number | null; branch: string | null; status: string; summary: string | null; updatedAt: string }[];
  deploys: { jobId: number; files: string | null; deployedAt: string; revertedAt: string | null; canaryStatus: string | null }[];
};

const STATUS_TONE: Record<string, "accent" | "warn" | "info" | "neutral" | "danger"> = {
  done: "accent",
  deployed: "accent",
  approved: "info",
  proposed: "info",
  pending: "neutral",
  skipped: "neutral",
  rejected: "danger",
  failed: "danger",
};

const STATUS_JA: Record<string, string> = {
  done: "完了",
  pending: "未着手",
  approved: "承認済み",
  proposed: "提案中",
  skipped: "見送り",
  deployed: "デプロイ済み",
  rejected: "却下",
  failed: "失敗",
  open: "未対応",
};

function ja(status: string): string {
  return STATUS_JA[status] ?? status;
}

/** 開発BOTのページにだけ出す固有パネル（監視対象・起票ロードマップ・開発ジョブ）。 */
export function DevBotPanels() {
  const { data: settings } = useFetch<SettingsView>("/settings");
  const { data: roadmap } = useFetch<RoadmapRow[]>("/data/roadmap");
  const { data: dev } = useFetch<DevJobsView>("/data/dev-jobs");

  const byStatus = new Map<string, number>();
  for (const r of roadmap ?? []) byStatus.set(r.status, (byStatus.get(r.status) ?? 0) + 1);

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card
        title="監視しているプロセス"
        desc="Discord上でオフライン表示になったら異常とみなします（人間の見え方と一致させるため）"
      >
        {(settings?.monitorTargets.length ?? 0) === 0 ? (
          <Empty>監視対象が設定されていません</Empty>
        ) : (
          <ul>
            {settings?.monitorTargets.map((t) => (
              <li key={t.name} className="border-t border-hairline px-4 py-3 first:border-t-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{t.name}</span>
                  <span className="font-mono text-2xs text-faint">{t.launchdLabel}</span>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  {t.presenceBotNames.length === 0 ? (
                    <Chip tone="warn">オンライン判定なし</Chip>
                  ) : (
                    t.presenceBotNames.map((n) => <Chip key={n}>{n} で判定</Chip>)
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="起票ロードマップ"
        desc="能力リクエストから生まれた開発項目。開発BOTが実装するものと、人間のセッションに渡すものがあります。"
        right={<span className="tnum text-2xs text-faint">全 {roadmap?.length ?? 0} 件</span>}
      >
        <div className="flex flex-wrap gap-2 border-b border-hairline px-4 py-3">
          {[...byStatus.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([status, n]) => (
              <Chip key={status} tone={STATUS_TONE[status] ?? "neutral"}>
                {ja(status)} {n}
              </Chip>
            ))}
        </div>
        <ul>
          {(roadmap ?? []).slice(0, 40).map((r) => (
            <li
              key={r.id}
              className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
            >
              <span className="tnum w-9 shrink-0 text-2xs text-faint">#{r.id}</span>
              <span className="min-w-0 flex-1 truncate">{r.title}</span>
              {r.route !== null && <span className="shrink-0 text-2xs text-faint">{r.route}</span>}
              <Chip tone={STATUS_TONE[r.status] ?? "neutral"}>{ja(r.status)}</Chip>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="開発ジョブ" desc="worktree で実装 → 承認 → デプロイ の履歴">
        {(dev?.jobs.length ?? 0) === 0 ? (
          <Empty>ジョブはありません</Empty>
        ) : (
          <ul>
            {dev?.jobs.slice(0, 10).map((j) => (
              <li
                key={j.id}
                className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
              >
                <span className="tnum w-9 shrink-0 text-2xs text-faint">#{j.id}</span>
                <span className="min-w-0 flex-1 truncate">
                  {j.summary ?? j.branch ?? "（要約なし）"}
                </span>
                <span className="tnum shrink-0 text-2xs text-faint">{jstStamp(j.updatedAt)}</span>
                <Chip tone={STATUS_TONE[j.status] ?? "neutral"}>{ja(j.status)}</Chip>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="デプロイ履歴" desc="24時間のカナリア観察でエラーログの急増を見張ります">
        {(dev?.deploys.length ?? 0) === 0 ? (
          <Empty>デプロイはありません</Empty>
        ) : (
          <ul>
            {dev?.deploys.map((d) => (
              <li
                key={`${d.jobId}-${d.deployedAt}`}
                className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
              >
                <span className="tnum w-9 shrink-0 text-2xs text-faint">#{d.jobId}</span>
                <span className="min-w-0 flex-1 truncate text-muted">{d.files ?? "—"}</span>
                <span className="tnum shrink-0 text-2xs text-faint">{jstStamp(d.deployedAt)}</span>
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
  );
}
