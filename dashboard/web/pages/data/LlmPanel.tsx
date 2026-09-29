import { Chip, ListState } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import { agentLabel, ja, jstDate, jstStamp } from "../../lib/format.ts";
import { PanelHead, Row, sec, SubHead, usd } from "./common.tsx";
import type { LlmCall, LlmDaily, LlmPurpose } from "./types.ts";

/** コスト: AI（claude）を何回・何の用途で呼んだか。 */
export function LlmPanel() {
  const q = useFetch<{
    daily: LlmDaily[];
    byPurpose: LlmPurpose[];
    recent: LlmCall[];
  }>("/data/llm");
  const d = q.data;
  return (
    <>
      <PanelHead
        title="AIの呼び出しとコスト"
        what="エージェントが裏でAI（claude）を呼んだ回数・かかった時間・コストの目安（定価換算）。急に増えていたら何かが空回りしています"
        how="使うモデルと考える深さは、全体設定の「使うAI」とエージェントのページで変えます"
      />
      <ul className="overflow-x-auto">
        <SubHead>日別（直近14日）</SubHead>
        {(d?.daily ?? []).length === 0 ? (
          <ListState q={q} empty="まだ記録がありません" />
        ) : (
          d?.daily.map((x) => (
            <Row key={x.day}>
              <span className="tnum w-24 shrink-0 font-medium">
                {jstDate(x.day)}
              </span>
              <span className="tnum w-16 shrink-0">{x.calls} 回</span>
              <span
                className={`tnum w-16 shrink-0 ${x.failed > 0 ? "text-danger" : "text-faint"}`}
              >
                失敗 {x.failed}
              </span>
              <span className="tnum w-20 shrink-0">{usd(x.costUsd)}</span>
              <span className="tnum w-24 shrink-0 text-muted">
                平均 {sec(x.avgMs)}
              </span>
              <span className="tnum min-w-0 flex-1 text-faint">
                最大 {sec(x.maxMs)}
              </span>
            </Row>
          ))
        )}
        <SubHead>用途別（直近7日・コスト順）</SubHead>
        {(d?.byPurpose ?? []).length === 0 ? (
          <ListState q={q} empty="まだ記録がありません" />
        ) : (
          d?.byPurpose.map((p) => (
            <Row key={`${p.agentId}-${p.purpose}`}>
              <span className="w-16 shrink-0 text-muted">
                {agentLabel(p.agentId ?? "?")}
              </span>
              <span className="w-32 shrink-0 font-medium">
                {ja(p.purpose ?? "other")}
              </span>
              <span className="tnum w-16 shrink-0">{p.calls} 回</span>
              <span
                className={`tnum w-16 shrink-0 ${p.failed > 0 ? "text-danger" : "text-faint"}`}
              >
                失敗 {p.failed}
              </span>
              <span className="tnum w-20 shrink-0">{usd(p.costUsd)}</span>
              <span className="tnum min-w-0 flex-1 text-muted">
                平均 {sec(p.avgMs)}
              </span>
            </Row>
          ))
        )}
        <SubHead>直近の呼び出し（失敗は理由つき）</SubHead>
        {(d?.recent ?? []).length === 0 ? (
          <ListState q={q} empty="まだ記録がありません" />
        ) : (
          d?.recent.map((r) => (
            <li
              key={r.id}
              className="border-t border-hairline px-4 py-2 text-xs"
            >
              <div className="tnum flex items-baseline gap-3">
                <span className="w-24 shrink-0 text-faint">
                  {jstStamp(r.createdAt)}
                </span>
                <span className="w-16 shrink-0 text-muted">
                  {agentLabel(r.agentId ?? "?")}
                </span>
                <span className="w-32 shrink-0 font-medium">
                  {ja(r.purpose ?? "other")}
                </span>
                <Chip tone={r.ok ? "neutral" : "danger"}>
                  {r.ok ? "成功" : "失敗"}
                </Chip>
                <span className="w-14 shrink-0">{sec(r.durationMs)}</span>
                <span className="w-16 shrink-0">{usd(r.costUsd)}</span>
                <span className="min-w-0 flex-1 truncate text-faint">
                  {r.model ?? ""} · {r.numTurns ?? 0}ターン · ツール{" "}
                  {r.toolCalls ?? 0}
                  {r.denials ? ` · 拒否 ${r.denials}` : ""}
                </span>
              </div>
              {r.error && (
                <div className="mt-1 pl-[108px] text-2xs text-danger">
                  {r.error}
                </div>
              )}
            </li>
          ))
        )}
      </ul>
    </>
  );
}
