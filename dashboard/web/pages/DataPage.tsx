import { useEffect, useState, type ReactNode } from "react";

import { Card, Metric, PageHeader } from "../components/ui.tsx";
import { useFetch } from "../lib/api.ts";
import {
  CapabilitiesPanel,
  FeedbackPanel,
  LearningPanel,
} from "./data/GrowthPanels.tsx";
import { PersonasPanel, SheetsPanel } from "./data/LinkPanels.tsx";
import { LlmPanel } from "./data/LlmPanel.tsx";
import { MissesPanel } from "./data/MissesPanel.tsx";
import { RulesPanel, TermsPanel } from "./data/MemoryPanels.tsx";
import { RemindersPanel, TasksPanel } from "./data/TrackingPanels.tsx";
import type { Summary } from "./data/types.ts";

type PanelId =
  | "rules"
  | "terms"
  | "tasks"
  | "reminders"
  | "feedback"
  | "capabilities"
  | "misses"
  | "advice"
  | "golden"
  | "colleague"
  | "personas"
  | "sheets"
  | "llm";

/**
 * 目的別のまとまり。タブを10個以上並べると何がどこにあるか分からないので、
 * 「エージェントが何を持っているか」の問いごとに見出しを立てる。
 */
const SECTIONS: { label: string; items: { id: PanelId; label: string }[] }[] = [
  {
    label: "覚えていること",
    items: [
      { id: "rules", label: "ルール記憶" },
      { id: "terms", label: "名前辞書・単語帳" },
    ],
  },
  {
    label: "追いかけていること",
    items: [
      { id: "tasks", label: "追跡タスク" },
      { id: "reminders", label: "リマインダー" },
    ],
  },
  {
    label: "成長の記録",
    items: [
      { id: "feedback", label: "評価（👍👎）" },
      { id: "capabilities", label: "能力リクエスト" },
      { id: "misses", label: "失敗と間違い" },
      { id: "advice", label: "改善メモ" },
      { id: "golden", label: "模範のQ&A" },
      { id: "colleague", label: "同僚としての一言" },
    ],
  },
  {
    label: "つながり",
    items: [
      { id: "personas", label: "Webhook人格" },
      { id: "sheets", label: "シート登録簿" },
    ],
  },
  { label: "コスト", items: [{ id: "llm", label: "AIの呼び出し" }] },
];

const ALL_IDS: string[] = SECTIONS.flatMap((s) => s.items.map((i) => i.id));

/** URL の #tasks のような指定で開けるようにする（他のページからリンクできる）。 */
function panelFromHash(): PanelId {
  const h = window.location.hash.replace("#", "");
  return ALL_IDS.includes(h) ? (h as PanelId) : "rules";
}

export function DataPage() {
  const [panel, setPanel] = useState<PanelId>(panelFromHash);
  const summaryQ = useFetch<Summary>("/data/summary");
  const summary = summaryQ.data;
  const c = summary?.counters;

  useEffect(() => {
    const onHash = () => setPanel(panelFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const open = (id: PanelId) => {
    setPanel(id);
    window.history.replaceState(null, "", `#${id}`);
  };

  const body: Record<PanelId, ReactNode> = {
    rules: <RulesPanel />,
    terms: <TermsPanel />,
    tasks: <TasksPanel />,
    reminders: <RemindersPanel />,
    feedback: <FeedbackPanel summary={summary} />,
    capabilities: <CapabilitiesPanel />,
    misses: <MissesPanel />,
    advice: <LearningPanel part="advice" />,
    golden: <LearningPanel part="golden" />,
    colleague: <LearningPanel part="colleague" />,
    personas: <PersonasPanel summary={summary} />,
    sheets: <SheetsPanel summary={summary} />,
    llm: <LlmPanel />,
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="データ"
        lead="エージェントが実際に覚えていること・追いかけていること・学んだこと。ここは見るだけのページで、変えるときはDiscordで話しかけます（各パネルの「変え方」を参照）。"
        tipsKey="data"
        tips={[
          "左の見出しは「エージェントに何を聞きたいか」で分けてあります。例: 何を覚えている？→「覚えていること」",
          "URL の末尾に #tasks のように付けると、そのパネルを直接開けます",
        ]}
      />

      <Card>
        <div className="grid grid-cols-2 gap-5 p-4 md:grid-cols-4">
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
          <Metric
            label="蓄積したメッセージ"
            value={(c?.messages ?? 0).toLocaleString()}
            sub={`${c?.channels ?? 0} チャンネル`}
          />
        </div>
      </Card>

      <div className="grid gap-6 md:grid-cols-[200px_minmax(0,1fr)]">
        <nav aria-label="データの種類" className="space-y-4 md:sticky md:top-4 md:self-start">
          {SECTIONS.map((s) => (
            <div key={s.label}>
              <div className="eyebrow px-2 pb-1">{s.label}</div>
              <ul>
                {s.items.map((i) => (
                  <li key={i.id}>
                    <button
                      type="button"
                      aria-current={panel === i.id ? "page" : undefined}
                      onClick={() => open(i.id)}
                      className={`focus-ring w-full rounded-md px-2 py-1.5 text-left text-[13px] transition-colors ${
                        panel === i.id
                          ? "bg-ink font-medium text-white"
                          : "text-muted hover:bg-canvas hover:text-ink"
                      }`}
                    >
                      {i.label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <Card className="min-w-0">{body[panel]}</Card>
      </div>
    </div>
  );
}
