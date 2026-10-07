import { Chip, ListState } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import { agentLabel, jstStamp } from "../../lib/format.ts";
import { LIST_MAX, MoreRow, PanelHead } from "./common.tsx";
import type { Miss } from "./types.ts";

/** 種類ごとの表示（core/misses.py の SOURCE_LABEL と揃える）。 */
const SOURCE: Record<
  string,
  { label: string; tone: "danger" | "warn" | "neutral" }
> = {
  ripple: { label: "波及チェックの案に❌", tone: "danger" },
  ripple_manual: { label: "自動では直せなかった", tone: "warn" },
  capability: { label: "頼まれたけどできなかった", tone: "warn" },
  tool_failed: { label: "ツールの失敗", tone: "danger" },
  fake_done: { label: "できたフリを検出", tone: "danger" },
  taught: { label: "教わって直した（見本）", tone: "neutral" },
  drill_breach: { label: "乗っ取り訓練で突破", tone: "danger" },
  quality_drop: { label: "回答品質が急に下がった", tone: "danger" },
  bg_task_failed: { label: "裏の作業が最後までできなかった", tone: "warn" },
};

/** 理由を聞かない種類（人の操作が起点ではないもの）。 */
const NO_REASON = new Set([
  "ripple_manual",
  "tool_failed",
  "fake_done",
  "drill_breach",
  "quality_drop",
]);

/**
 * 成長の記録: 失敗と間違いの台帳。❌された提案と、そのとき人が教えてくれた理由、
 * 自動ではやり切れなかったことを貯めて、改善の起票の材料にする。
 */
export function MissesPanel() {
  const q = useFetch<Miss[]>("/data/misses");
  const rows = q.data ?? [];
  return (
    <>
      <PanelHead
        title="失敗と間違い"
        what="❌された提案と教えてもらった理由、自動ではやり切れなかったこと、ツールの失敗、できたフリの検出、点検で赤（乗っ取り訓練の突破・回答品質の急落）を貯めています。同じ種類が3件たまると（❌は理由つきのものだけ数えます）開発BOTへの改善の起票になります。点検で赤は1件で即起票します"
        how="❌を押すとエージェントが一度だけ理由を聞きます。その投稿（または❌した提案）に返信で一言答えると、ここに残ります（任意）"
      />
      {rows.length === 0 ? (
        <ListState q={q} empty="まだ記録はありません" />
      ) : (
        <ul>
          {rows.slice(0, LIST_MAX).map((m) => (
            <MissItem key={m.id} m={m} />
          ))}
          <MoreRow total={rows.length} shown={LIST_MAX} />
        </ul>
      )}
    </>
  );
}

function MissItem({ m }: { m: Miss }) {
  const src = SOURCE[m.source] ?? { label: m.source, tone: "neutral" as const };
  return (
    <li className="border-t border-hairline px-4 py-2.5 text-xs first:border-t-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="tnum text-2xs text-faint">{jstStamp(m.createdAt)}</span>
        <Chip tone={src.tone}>{src.label}</Chip>
        {m.filedCapId !== null && <Chip tone="info">起票#{m.filedCapId}</Chip>}
        <span className="min-w-0 flex-1 font-medium">{m.context ?? ""}</span>
        {m.agentId && (
          <span className="text-2xs text-faint">{agentLabel(m.agentId)}</span>
        )}
      </div>
      {m.detail && (
        <div className="mt-1 whitespace-pre-wrap text-muted">{m.detail}</div>
      )}
      {m.reason ? (
        <div className="mt-1">
          <span className="text-2xs font-semibold text-accent-deep">
            {m.source === "taught" ? "人の指示:" : "理由:"}
          </span>{" "}
          {m.reason}
        </div>
      ) : (
        !NO_REASON.has(m.source) && (
          <div className="mt-1 text-2xs text-faint">理由はまだもらっていません</div>
        )
      )}
    </li>
  );
}
