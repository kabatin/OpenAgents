import { Chip, Empty } from "../../components/ui.tsx";
import { PanelHead, Row } from "./common.tsx";
import type { Summary } from "./types.ts";

/** つながり①: Webhook で動く追加の人格。 */
export function PersonasPanel({ summary }: { summary: Summary | null }) {
  const agents = summary?.counters.webhookAgents ?? [];
  return (
    <>
      <PanelHead
        title="Webhook人格"
        what="Botアカウントとは別に、Webhook（アイコンと名前だけ差し替えて話す仕組み）で動いているAIの人格"
        how="AI人事の採用提案に👍するか、platforms/discord/manage_agents.py で追加・退役します"
      />
      {agents.length === 0 ? (
        <Empty>Webhook人格はいません</Empty>
      ) : (
        <ul>
          {agents.map((a) => (
            <Row key={a.id}>
              <span className="font-medium">{a.name}</span>
              <span className="font-mono text-2xs text-faint">{a.id}</span>
              <span className="ml-auto">
                <Chip tone={a.status === "active" ? "accent" : "neutral"}>
                  {a.status === "active" ? "稼働中" : "退役"}
                </Chip>
              </span>
            </Row>
          ))}
        </ul>
      )}
    </>
  );
}

/** つながり②: 読み書きしてよいスプレッドシート。 */
export function SheetsPanel({ summary }: { summary: Summary | null }) {
  const sheets = summary?.counters.sheetRegistry ?? [];
  return (
    <>
      <PanelHead
        title="シート登録簿"
        what="シート連携（integrations/）が読み書きしてよいスプレッドシートの一覧。登録していないシートには触りません"
        how="シート連携を入れている場合、管理者がDiscordで「このシートを〜として登録して」と頼みます"
      />
      {sheets.length === 0 ? (
        <Empty>登録されたシートはありません</Empty>
      ) : (
        <ul>
          {sheets.map((s) => (
            <Row key={s.alias}>
              <span className="font-medium">{s.alias}</span>
              <span className="min-w-0 flex-1 truncate text-muted">
                {s.title ?? "—"}
              </span>
              <Chip tone={s.mode === "rw" ? "warn" : "neutral"}>
                {s.mode === "rw" ? "読み書き" : "読み取り"}
              </Chip>
              {s.active === 0 && <Chip>無効</Chip>}
            </Row>
          ))}
        </ul>
      )}
    </>
  );
}
