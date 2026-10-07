import { Chip, ListState } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import { agentLabel, jstStamp } from "../../lib/format.ts";
import { LIST_MAX, MoreRow, PanelHead } from "./common.tsx";

type BgTask = {
  id: number;
  agentId: string | null;
  instruction: string | null;
  status: string;
  summary: string | null;
  createdAt: string | null;
  finishedAt: string | null;
};

const STATUS: Record<string, { label: string; tone: "info" | "accent" | "danger" | "warn" }> = {
  running: { label: "作業中", tone: "info" },
  done: { label: "完了", tone: "accent" },
  failed: { label: "最後までできなかった", tone: "danger" },
  interrupted: { label: "再起動で中断", tone: "warn" },
};

/** 裏の作業: 引き受けた重い作業と、その結果（完了報告の本文）・失敗の理由。 */
export function BgTasksPanel() {
  const q = useFetch<BgTask[]>("/data/bg-tasks");
  const rows = q.data ?? [];
  return (
    <>
      <PanelHead
        title="裏の作業"
        what="時間がかかる依頼を「取りかかります」と引き受けて、スレッドで裏で進めた記録です。結果の本文と、うまくいかなかったときの理由を残しています"
        how="Discordで資料や表づくり・調べものを頼むと、必要に応じて裏の作業になります。使うかどうかはエージェントの設定「重い作業は裏で進める」で切り替えます"
      />
      {rows.length === 0 ? (
        <ListState q={q} empty="まだ裏の作業はありません" />
      ) : (
        <ul>
          {rows.slice(0, LIST_MAX).map((t) => {
            const st = STATUS[t.status] ?? { label: t.status, tone: "info" as const };
            return (
              <li key={t.id} className="border-t border-hairline px-4 py-3 text-xs first:border-t-0">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="tnum text-2xs text-faint">#{t.id}</span>
                  <span className="tnum text-2xs text-faint">{jstStamp(t.createdAt)}</span>
                  <Chip tone={st.tone}>{st.label}</Chip>
                  <span className="min-w-0 flex-1 font-medium">{t.instruction ?? ""}</span>
                  {t.agentId !== null && (
                    <span className="text-2xs text-faint">{agentLabel(t.agentId)}</span>
                  )}
                </div>
                {t.summary && (
                  <details className="mt-1.5">
                    <summary className="cursor-pointer text-2xs text-muted">結果・理由を見る</summary>
                    <pre className="mt-1 whitespace-pre-wrap break-words rounded-md border border-hairline bg-canvas px-3 py-2 font-sans text-xs leading-relaxed">
                      {t.summary}
                    </pre>
                  </details>
                )}
              </li>
            );
          })}
          <MoreRow total={rows.length} shown={LIST_MAX} />
        </ul>
      )}
    </>
  );
}
