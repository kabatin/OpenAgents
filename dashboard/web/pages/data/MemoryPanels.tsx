import { Chip, ListState } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import { jstDate } from "../../lib/format.ts";
import {
  IdCell,
  LIST_MAX,
  MoreRow,
  PanelHead,
  Row,
  scopeLabel,
  SubHead,
} from "./common.tsx";
import type { FactEntry, GlossaryEntry, Rule, TermEntry } from "./types.ts";

/** 覚えていること①: 会話で教わったルール。 */
export function RulesPanel() {
  const q = useFetch<Rule[]>("/data/rules");
  const rules = q.data ?? [];
  return (
    <>
      <PanelHead
        title="ルール記憶"
        what="「今後は〜して」と会話で教わった決まりごと。回答のたびに、そのチャンネル・その人に合うものが効きます"
        how="Discordで「今後は〜して」「さっきのルールは無しで」と話しかけます"
      />
      <ul>
        {rules.length === 0 ? (
          <ListState q={q} empty="ルールはありません" />
        ) : (
          rules.slice(0, LIST_MAX).map((r) => (
            <Row key={r.id}>
              <IdCell id={r.id} />
              <Chip tone={r.scope === "global" ? "info" : "neutral"}>
                {r.scopeName == null
                  ? scopeLabel(r.scope)
                  : `${scopeLabel(r.scope)}: ${r.scopeName}`}
              </Chip>
              <span
                className={`min-w-0 flex-1 ${r.active ? "" : "text-faint line-through"}`}
              >
                {r.ruleText}
              </span>
              {r.expiresAt !== null && (
                <Chip tone="warn">{jstDate(r.expiresAt)} まで</Chip>
              )}
              {r.active === 0 && <Chip>無効</Chip>}
            </Row>
          ))
        )}
        <MoreRow total={rules.length} shown={LIST_MAX} />
      </ul>
    </>
  );
}

const FACT_STATUS: Record<string, string> = {
  superseded: "置き換え済み",
  cancelled: "取り消し済み",
};

/** 覚えていること②: いまの状況（教わったもの・会話から拾ったもの）。 */
export function FactsPanel() {
  const q = useFetch<FactEntry[]>("/data/facts");
  const facts = q.data ?? [];
  return (
    <>
      <PanelHead
        title="事実台帳"
        what="「送料は800円に変更」「登録は済んだ」のような、いまの状況の記録。「いまどうなってる？」に答えるとき、古い記録より優先して使います。「自分で拾った」は、エージェントが人同士の会話から拾ったものです"
        how="Discordで「〜は〜になったよ」と教えます。間違っていたら「事実の3番を取り消して」と話しかけます"
      />
      <ul>
        {facts.length === 0 ? (
          <ListState q={q} empty="事実はまだありません" />
        ) : (
          facts.slice(0, LIST_MAX).map((f) => {
            const live = f.status === "active";
            return (
              <Row key={f.id}>
                <IdCell id={f.id} />
                <Chip tone={f.sourceKind === "extracted" ? "warn" : "info"}>
                  {f.sourceKind === "extracted" ? "自分で拾った" : "教わった"}
                </Chip>
                <span
                  className={`min-w-0 flex-1 ${live ? "" : "text-faint line-through"}`}
                >
                  <span className="font-medium">{f.topic ?? ""}</span>
                  {"　"}
                  {f.fact ?? ""}
                </span>
                <span className="shrink-0 text-2xs text-muted">
                  {[f.statedBy, f.createdAt?.slice(5, 10).replace("-", "/")]
                    .filter(Boolean)
                    .join("・")}
                </span>
                {!live && <Chip>{FACT_STATUS[f.status ?? ""] ?? "無効"}</Chip>}
              </Row>
            );
          })
        )}
        <MoreRow total={facts.length} shown={LIST_MAX} />
      </ul>
    </>
  );
}

/** 覚えていること③: 固有名詞と、誤表記の置き換え。 */
export function TermsPanel() {
  const q = useFetch<{ terms: TermEntry[]; glossary: GlossaryEntry[] }>(
    "/data/dictionary",
  );
  return (
    <>
      <PanelHead
        title="名前辞書・単語帳"
        what="人やチームの呼び名（議事録の話者名もここに寄せます）と、よくある誤表記→正しい表記の置き換え表"
        how="Discordで「〜は〜のことだと覚えて」「〜は〜の誤記」と話しかけます"
      />
      <ul>
        <SubHead>固有名詞</SubHead>
        {(q.data?.terms ?? []).length === 0 ? (
          <ListState q={q} empty="固有名詞は登録されていません" />
        ) : (
          q.data?.terms.map((t) => (
            <Row key={t.term}>
              <span className="w-44 shrink-0 font-medium">{t.term}</span>
              <span className="min-w-0 flex-1 text-muted">
                {t.description ?? ""}
              </span>
            </Row>
          ))
        )}
        <SubHead>単語帳（誤表記 → 正表記）</SubHead>
        {(q.data?.glossary ?? []).length === 0 ? (
          <ListState q={q} empty="単語帳は空です" />
        ) : (
          q.data?.glossary.map((g) => (
            <Row key={g.wrong}>
              <span className="w-44 shrink-0 text-faint line-through">
                {g.wrong}
              </span>
              <span className="min-w-0 flex-1 font-medium">
                → {g.correct ?? ""}
              </span>
            </Row>
          ))
        )}
      </ul>
    </>
  );
}
