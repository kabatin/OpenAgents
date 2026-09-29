import { Chip, Empty, ListState } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import { agentLabel, ja, jstStamp, plainDiscord } from "../../lib/format.ts";
import { IdCell, LIST_MAX, MoreRow, PanelHead, Row } from "./common.tsx";
import type {
  AdviceRow,
  Capability,
  GoldenRow,
  ShadowRow,
  Summary,
} from "./types.ts";

/** 成長の記録①: 投稿に付いた👍👎。 */
export function FeedbackPanel({ summary }: { summary: Summary | null }) {
  const feedback = summary?.counters.feedback ?? [];
  return (
    <>
      <PanelHead
        title="評価（👍👎）"
        what="エージェントの投稿に人が付けた👍👎の数。良し悪しの物差しの原料です（✅=登録／❌=見送りのような操作のリアクションは数えていません）"
        how="Discordで投稿に👍・👎を付けるだけです"
      />
      {feedback.length === 0 ? (
        <Empty>まだ評価はありません</Empty>
      ) : (
        <div className="flex flex-wrap gap-8 p-4">
          {feedback.map((f) => (
            <div key={f.agentId}>
              <div className="eyebrow">{agentLabel(f.agentId)}</div>
              <div className="tnum mt-1 flex items-baseline gap-3">
                <span className="text-xl font-semibold text-accent-deep">
                  👍 {f.up}
                </span>
                <span
                  className={`text-sm ${f.down > 0 ? "text-danger" : "text-faint"}`}
                >
                  👎 {f.down}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

const CAP_STATUS: Record<
  string,
  { label: string; tone: "accent" | "danger" | "warn" | "neutral" }
> = {
  deployed: { label: "実装済み", tone: "accent" },
  rejected: { label: "見送り", tone: "danger" },
  open: { label: "未対応", tone: "warn" },
};

/** 成長の記録②: 「できない」と言った記録（能力リクエスト）。 */
export function CapabilitiesPanel() {
  const q = useFetch<Capability[]>("/data/capabilities");
  const caps = q.data ?? [];
  return (
    <>
      <PanelHead
        title="能力リクエスト（起票）"
        what="頼まれたけれどできなかったことを、エージェント自身が記録したもの。開発BOTの開発候補になります"
        how="実装するかは、開発BOTの提案に👍／👎で答えます（開発BOTを使っていなければ人が対応します）"
      />
      <ul>
        {caps.length === 0 ? (
          <ListState q={q} empty="能力リクエストはありません" />
        ) : (
          caps.slice(0, LIST_MAX).map((r) => {
            const st = CAP_STATUS[r.status] ?? {
              label: ja(r.status),
              tone: "neutral" as const,
            };
            return (
              <Row key={r.id}>
                <IdCell id={r.id} />
                <span className="w-14 shrink-0 text-2xs text-muted">
                  {agentLabel(r.agentId)}
                </span>
                <span className="min-w-0 flex-1">{r.description}</span>
                <Chip tone={st.tone}>{st.label}</Chip>
              </Row>
            );
          })
        )}
        <MoreRow total={caps.length} shown={LIST_MAX} />
      </ul>
    </>
  );
}

type LearningView = {
  shadow: ShadowRow[];
  advice: AdviceRow[];
  golden: GoldenRow[];
};

const GOLDEN_TONE: Record<string, "info" | "warn" | "neutral"> = {
  curated: "info",
  candidate: "warn",
};

type LearningPart = "advice" | "golden" | "colleague";

const LEARNING_HEAD: Record<
  LearningPart,
  { title: string; what: string; how: string }
> = {
  advice: {
    title: "改善メモ",
    what: "低評価だった回答の共通点から、週1回まとめた「次から気をつけること」。回答のたびに読み込まれています。3週続けて出たものは恒久ルールへの格上げを提案します",
    how: "毎週自動で入れ替わります。格上げの提案には✅／❌で答えます",
  },
  golden: {
    title: "模範のQ&A",
    what: "回答品質の物差し。👍が付いた回答を自動で集め（自動捕獲）、人が選んだもの（採用）で毎週の回帰テストをしています",
    how: "候補は python -m core.golden_curate --propose で作り、--approve／--reject で番号を指定します",
  },
  colleague: {
    title: "同僚としての一言",
    what: "情報ではなく場への参加として一言添えた（または添えかけてコードが止めた）記録。1日1回まで。「事実を述べたら黙る」「長すぎたら黙る」で縛っており、👎が続いた型は自動で控えます",
    how: "エージェントのページの「同僚としての一言」で調整します",
  },
};

const GOLDEN_MAX = 30;

/** 成長の記録③〜⑥: 自分で学んだこと（1つの取得を4つの項目で見せる）。 */
export function LearningPanel({ part }: { part: LearningPart }) {
  const q = useFetch<LearningView>("/data/observations");
  const d = q.data;
  const head = LEARNING_HEAD[part];
  return (
    <>
      <PanelHead title={head.title} what={head.what} how={head.how} />
      <ul>
        {part === "advice" &&
          ((d?.advice ?? []).length === 0 ? (
            <ListState q={q} empty="改善メモはまだありません" />
          ) : (
            d?.advice.map((a) => (
              <Row key={a.id}>
                <span className="w-16 shrink-0 text-faint">
                  {agentLabel(a.agentId)}
                </span>
                <span className="min-w-0 flex-1">{a.text}</span>
                <Chip
                  tone={
                    a.streak >= 3
                      ? "danger"
                      : a.streak >= 2
                        ? "warn"
                        : "neutral"
                  }
                >
                  {a.streak}週連続
                </Chip>
              </Row>
            ))
          ))}

        {part === "golden" && (
          <>
            {(d?.golden ?? []).length === 0 ? (
              <ListState q={q} empty="模範Q&Aはまだありません" />
            ) : (
              d?.golden.slice(0, GOLDEN_MAX).map((g) => (
                <li
                  key={g.id}
                  className="border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
                >
                  <div className="flex items-baseline gap-2.5">
                    <IdCell id={g.id} />
                    <Chip tone={GOLDEN_TONE[g.status ?? ""] ?? "neutral"}>
                      {ja(g.status)}
                    </Chip>
                    <span className="min-w-0 flex-1 font-medium">
                      {plainDiscord(g.question)}
                    </span>
                    {g.note && (
                      <span className="shrink-0 text-faint">{g.note}</span>
                    )}
                  </div>
                  <div className="mt-1 line-clamp-2 break-all pl-[46px] text-muted">
                    {plainDiscord(g.answer)}
                  </div>
                </li>
              ))
            )}
            <MoreRow total={d?.golden.length ?? 0} shown={GOLDEN_MAX} />
          </>
        )}


        {part === "colleague" &&
          ((d?.shadow ?? []).length === 0 ? (
            <ListState q={q} empty="まだ記録はありません" />
          ) : (
            d?.shadow.map((row, i) => (
              <li
                key={`${row.triggerMessageId ?? i}-${i}`}
                className="border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
              >
                <div className="flex items-baseline gap-2 text-2xs text-faint">
                  <span>{jstStamp(row.createdAt)}</span>
                  <span>#{row.channel ?? "?"}</span>
                  <span>{row.author ?? "?"}</span>
                  <Chip
                    tone={
                      row.action === "spoke"
                        ? "accent"
                        : row.action === "shadow"
                          ? "neutral"
                          : "warn"
                    }
                  >
                    {row.action === "spoke"
                      ? "発言した"
                      : row.action === "shadow"
                        ? "シャドー"
                        : "止めた"}
                  </Chip>
                  <span className="ml-auto">{agentLabel(row.agentId)}</span>
                </div>
                <div className="mt-1 text-muted">
                  「{plainDiscord(row.trigger)}」
                </div>
                <div className="mt-1">→ {plainDiscord(row.detail)}</div>
              </li>
            ))
          ))}
      </ul>
    </>
  );
}
