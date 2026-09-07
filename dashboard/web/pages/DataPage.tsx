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

type Term = {
  term: string;
  description: string | null;
  createdBy: string | null;
  createdAt: string | null;
};

type GlossaryPair = {
  wrong: string;
  correct: string | null;
  createdBy: string | null;
  createdAt: string | null;
};

type Dictionary = { terms: Term[]; glossary: GlossaryPair[] };

type ShadowRow = {
  agentId: string;
  kind: string;
  action: string;
  channel: string | null;
  author: string | null;
  trigger: string | null;
  detail: string | null;
  createdAt: string | null;
  channelId: number | null;
  triggerMessageId: number | null;
};

type AdviceRow = {
  id: number;
  agentId: string;
  text: string;
  streak: number;
  createdAt: string | null;
};
type GoldenRow = {
  id: number;
  agentId: string | null;
  kind: string | null;
  status: string | null;
  question: string | null;
  answer: string | null;
  sourceLink: string | null;
  note: string | null;
  createdAt: string | null;
};

type TaskRow = {
  key: string;
  kind: "action" | "homework" | "reminder";
  id: number;
  task: string | null;
  owner: string | null;
  due: string | null;
  status: string | null;
  stage: string | null;
};

const TABS = [
  { id: "rules", label: "ルール記憶" },
  { id: "dictionary", label: "名前辞書" },
  { id: "tasks", label: "追跡タスク" },
  { id: "reminders", label: "リマインダー" },
  { id: "capabilities", label: "能力リクエスト" },
  { id: "personas", label: "Webhook人格" },
  { id: "sheets", label: "シート登録簿" },
  { id: "learning", label: "学びと実験" },
  { id: "llm", label: "LLM呼び出し" },
] as const;

type TabId = (typeof TABS)[number]["id"];

type LlmDaily = {
  day: string;
  calls: number;
  failed: number;
  costUsd: number;
  avgMs: number | null;
  maxMs: number | null;
  cacheReadTokens: number;
  outputTokens: number;
};
type LlmPurpose = {
  agentId: string | null;
  purpose: string | null;
  calls: number;
  failed: number;
  costUsd: number;
  avgMs: number | null;
};
type LlmCall = {
  id: number;
  agentId: string | null;
  purpose: string | null;
  model: string | null;
  ok: number | null;
  error: string | null;
  durationMs: number | null;
  numTurns: number | null;
  costUsd: number | null;
  toolCalls: number | null;
  denials: number | null;
  createdAt: string | null;
};

function usd(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `$${n.toFixed(3)}`;
}
function sec(ms: number | null | undefined): string {
  return ms === null || ms === undefined ? "—" : `${(ms / 1000).toFixed(1)}s`;
}

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
  const { data: tasks } = useFetch<{ items: TaskRow[] }>(tab === "tasks" ? "/data/tasks" : null);
  const { data: dict } = useFetch<Dictionary>(tab === "dictionary" ? "/data/dictionary" : null);
  const { data: learning } = useFetch<{ shadow: ShadowRow[]; advice: AdviceRow[]; golden: GoldenRow[] }>(
    tab === "learning" ? "/data/observations" : null,
  );
  const { data: llm } = useFetch<{ daily: LlmDaily[]; byPurpose: LlmPurpose[]; recent: LlmCall[] }>(
    tab === "llm" ? "/data/llm" : null,
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

        {tab === "dictionary" && (
          <div>
            <div className="px-4 py-3">
              <div className="eyebrow">固有名詞辞書</div>
              <p className="mt-1 max-w-[60ch] text-2xs leading-relaxed text-muted">
                正式な表記そのものを覚えさせるもの。登録しておくと、音が近いだけの
                知らない誤変換もAIがこの表記へ寄せます。
              </p>
            </div>
            <ul>
              {(dict?.terms ?? []).length === 0 ? (
                <Empty>登録された固有名詞はありません</Empty>
              ) : (
                dict?.terms.map((t) => (
                  <li
                    key={t.term}
                    className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2.5 text-xs"
                  >
                    <span className="w-32 shrink-0 font-medium">{t.term}</span>
                    <span className="min-w-0 flex-1 text-muted">{t.description || "—"}</span>
                    {t.createdBy !== null && (
                      <span className="shrink-0 text-2xs text-faint">{t.createdBy}</span>
                    )}
                  </li>
                ))
              )}
            </ul>

            <div className="border-t border-hairline px-4 py-3">
              <div className="eyebrow">単語帳</div>
              <p className="mt-1 max-w-[60ch] text-2xs leading-relaxed text-muted">
                「この誤変換はこう直す」の対応表。こちらはAIの判断を挟まず、
                決め打ちで置き換えます。
              </p>
            </div>
            <ul>
              {(dict?.glossary ?? []).length === 0 ? (
                <Empty>登録された単語はありません</Empty>
              ) : (
                dict?.glossary.map((g) => (
                  <li
                    key={g.wrong}
                    className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2.5 text-xs"
                  >
                    <span className="w-32 shrink-0 text-faint line-through">{g.wrong}</span>
                    <span className="shrink-0 text-faint">→</span>
                    <span className="min-w-0 flex-1 font-medium">{g.correct || "—"}</span>
                    {g.createdBy !== null && (
                      <span className="shrink-0 text-2xs text-faint">{g.createdBy}</span>
                    )}
                  </li>
                ))
              )}
            </ul>
          </div>
        )}

        {tab === "llm" && (
          <div className="pb-2">
            <div className="eyebrow px-4 pb-1 pt-3">
              日別（claude CLI の起動 1回＝1行。コストは CLI が申告する定価換算・14日）
            </div>
            <ul>
              {(llm?.daily ?? []).length === 0 ? (
                <Empty>まだ記録がありません（再起動後の呼び出しから貯まります）</Empty>
              ) : (
                llm?.daily.map((d) => (
                  <li
                    key={d.day}
                    className="tnum flex items-baseline gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                  >
                    <span className="w-24 shrink-0 font-medium">{d.day}</span>
                    <span className="w-16 shrink-0">{d.calls} 回</span>
                    <span className={`w-16 shrink-0 ${d.failed > 0 ? "text-danger" : "text-faint"}`}>
                      失敗 {d.failed}
                    </span>
                    <span className="w-20 shrink-0">{usd(d.costUsd)}</span>
                    <span className="w-24 shrink-0 text-muted">平均 {sec(d.avgMs)}</span>
                    <span className="w-24 shrink-0 text-muted">最大 {sec(d.maxMs)}</span>
                    <span className="min-w-0 flex-1 text-faint">
                      cache読 {d.cacheReadTokens.toLocaleString()} / 出力 {d.outputTokens.toLocaleString()} tok
                    </span>
                  </li>
                ))
              )}
            </ul>
            <div className="eyebrow px-4 pb-1 pt-4">用途別（直近7日・コスト順）</div>
            <ul>
              {(llm?.byPurpose ?? []).length === 0 ? (
                <Empty>まだ記録がありません</Empty>
              ) : (
                llm?.byPurpose.map((p) => (
                  <li
                    key={`${p.agentId}-${p.purpose}`}
                    className="tnum flex items-baseline gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                  >
                    <span className="w-20 shrink-0 text-muted">{agentLabel(p.agentId ?? "?")}</span>
                    <span className="w-28 shrink-0 font-medium">{p.purpose ?? "other"}</span>
                    <span className="w-16 shrink-0">{p.calls} 回</span>
                    <span className={`w-16 shrink-0 ${p.failed > 0 ? "text-danger" : "text-faint"}`}>
                      失敗 {p.failed}
                    </span>
                    <span className="w-20 shrink-0">{usd(p.costUsd)}</span>
                    <span className="min-w-0 flex-1 text-muted">平均 {sec(p.avgMs)}</span>
                  </li>
                ))
              )}
            </ul>
            <div className="eyebrow px-4 pb-1 pt-4">直近の呼び出し（失敗は理由つき）</div>
            <ul>
              {(llm?.recent ?? []).length === 0 ? (
                <Empty>まだ記録がありません</Empty>
              ) : (
                llm?.recent.map((r) => (
                  <li key={r.id} className="border-t border-hairline px-4 py-2 text-xs first:border-t-0">
                    <div className="tnum flex items-baseline gap-3">
                      <span className="w-24 shrink-0 text-faint">{jstStamp(r.createdAt)}</span>
                      <span className="w-20 shrink-0 text-muted">{agentLabel(r.agentId ?? "?")}</span>
                      <span className="w-28 shrink-0 font-medium">{r.purpose ?? "other"}</span>
                      <Chip tone={r.ok ? "neutral" : "danger"}>{r.ok ? "ok" : "失敗"}</Chip>
                      <span className="w-16 shrink-0">{sec(r.durationMs)}</span>
                      <span className="w-20 shrink-0">{usd(r.costUsd)}</span>
                      <span className="min-w-0 flex-1 text-faint">
                        {r.model ?? ""} · {r.numTurns ?? 0}ターン · ツール {r.toolCalls ?? 0}
                        {r.denials ? ` · 拒否 ${r.denials}` : ""}
                      </span>
                    </div>
                    {r.error && <div className="mt-1 pl-24 text-2xs text-danger">{r.error}</div>}
                  </li>
                ))
              )}
            </ul>
          </div>
        )}

        {tab === "learning" && (
          <div className="pb-2">
            <div className="eyebrow px-4 pb-1 pt-3">
              模範Q&A（回帰テストの物差し）。candidate=採用待ち / curated=採用 /
              active=👍自動捕獲。採用・不採用は `python -m core.golden_curate` で番号指定
            </div>
            <ul>
              {(learning?.golden ?? []).length === 0 ? (
                <Empty>模範Q&Aはまだありません</Empty>
              ) : (
                learning?.golden.map((g) => (
                  <li key={g.id} className="border-t border-hairline px-4 py-2 text-xs first:border-t-0">
                    <div className="flex items-baseline gap-2.5">
                      <span className="tnum w-8 shrink-0 text-2xs text-faint">#{g.id}</span>
                      <Chip
                        tone={
                          g.status === "curated" ? "info" : g.status === "candidate" ? "warn" : "neutral"
                        }
                      >
                        {g.status}
                      </Chip>
                      <span className="min-w-0 flex-1 font-medium">{g.question}</span>
                      {g.note && <span className="shrink-0 text-faint">{g.note}</span>}
                    </div>
                    <div className="mt-1 pl-10 text-muted">{g.answer}</div>
                  </li>
                ))
              )}
            </ul>
            <div className="eyebrow px-4 pb-1 pt-4">
              いま効いている自己改善メモ（週次蒸留・回答時に常時注入／3週連続で
              恒久ルールへ格上げ提案）
            </div>
            <ul>
              {(learning?.advice ?? []).length === 0 ? (
                <Empty>蒸留された助言はまだありません</Empty>
              ) : (
                learning?.advice.map((a) => (
                  <li
                    key={a.id}
                    className="flex items-baseline gap-2.5 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                  >
                    <span className="w-20 shrink-0 text-faint">{agentLabel(a.agentId)}</span>
                    <span className="min-w-0 flex-1">{a.text}</span>
                    <Chip tone={a.streak >= 3 ? "danger" : a.streak >= 2 ? "warn" : "neutral"}>
                      {a.streak}週連続
                    </Chip>
                  </li>
                ))
              )}
            </ul>

            <div className="eyebrow px-4 pb-1 pt-5">
              同僚としての一言（⑤colleague）
            </div>
            <p className="px-4 pb-2 text-2xs leading-relaxed text-muted">
              まずシャドー（投稿せず記録のみ）で試し、良さそうだったので本採用した枠です。
              有効にすると1日1回まで実際に発言します。「事実を述べたら黙る」
              「長すぎたら黙る」で縛っており、コードが止めたものは{" "}
              <span className="font-medium">止めた</span> と表示されます。
              👎が続いた型は自動で抑制されます。
            </p>
            <ul>
              {(learning?.shadow ?? []).length === 0 ? (
                <Empty>まだ記録はありません（一言添えたくなる場面がなかった）</Empty>
              ) : (
                learning?.shadow.map((row, i) => (
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
                    <div className="mt-1 text-muted">「{row.trigger ?? ""}」</div>
                    <div className="mt-1">→ {row.detail ?? ""}</div>
                  </li>
                ))
              )}
            </ul>
          </div>
        )}

        {tab === "tasks" && (
          <div className="pb-2">
            <div className="eyebrow px-4 pb-1 pt-3">
              追跡中のタスク（A=議事録TODO / H=宿題 / R=リマインダー）。
              変更は Discord で「H27 を完了に」「A6 を 9/12 に」のように話しかける
            </div>
            <ul>
              {(tasks?.items ?? []).length === 0 ? (
                <Empty>追跡中のタスクはありません</Empty>
              ) : (
                tasks?.items.map((t) => (
                  <li key={t.key} className="border-t border-hairline px-4 py-2 text-xs first:border-t-0">
                    <div className="flex items-baseline gap-2.5">
                      <span className="tnum w-10 shrink-0 font-medium text-accent-deep">{t.key}</span>
                      <Chip tone={t.status === "stale" ? "warn" : "info"}>
                        {{ action: "議事録", homework: "宿題", reminder: "リマインド" }[t.kind]}
                      </Chip>
                      <span className="tnum w-24 shrink-0 text-muted">{t.due ?? "未定"}</span>
                      <span className="min-w-0 flex-1">{t.task}</span>
                      {t.owner && <span className="shrink-0 text-faint">{t.owner}</span>}
                      <span className="shrink-0 text-2xs text-faint">{t.stage}</span>
                    </div>
                  </li>
                ))
              )}
            </ul>
          </div>
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
