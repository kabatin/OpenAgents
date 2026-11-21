import { Link } from "react-router-dom";

import { Card, ListState, Metric } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import {
  summarizeRequests,
  type RequestRow,
  type RequestSummary,
} from "../../lib/requests.ts";
import { WEEKS_DEFAULT } from "../../lib/weeks.ts";
import { PanelHead, SubHead } from "./common.tsx";

/** 頼まれごとの種類の表示名（web/lib/requests.ts の categoryOf）。 */
const CATEGORY: Record<string, string> = {
  reminder: "リマインダー",
  task: "追跡タスクの更新",
  teach: "覚えさせる（単語帳・事実・ルール）",
  recall: "過去の会話を調べる",
  other: "その他",
  none: "会話だけで返した",
};

type Log = { today: string; rows: RequestRow[] };

function weekLabel(monday: string): string {
  return `${Number(monday.slice(5, 7))}/${Number(monday.slice(8, 10))}〜`;
}

function summarize(log: Log | null): RequestSummary | null {
  return log === null
    ? null
    : summarizeRequests(log.rows, log.today, WEEKS_DEFAULT);
}

const NOTE =
  "エージェントが名前を呼ばれて答えた回数です（ツールで答える方式の回答が対象）。Botからの呼びかけと、シャドーの試し書きは数えません。";

/** データページのパネル。週ごとの件数・種類・頼んだ人。 */
export function RequestsPanel() {
  const q = useFetch<Log>("/data/requests");
  const s = summarize(q.data);
  const head = (
    <PanelHead
      title="頼まれごと"
      what="人から頼まれた回数と、その中身。投稿の数ではなく「使われているか」を見る場所です"
      how="ここは数えているだけです。増やすには、Discordでエージェントの名前を呼んで頼みます"
    />
  );
  if (s === null || s.total.asked === 0) {
    return (
      <>
        {head}
        <ListState
          q={q}
          empty={`直近${WEEKS_DEFAULT}週は、頼まれた記録がありません`}
        />
      </>
    );
  }
  return (
    <>
      {head}
      <SubHead>直近{s.weeks.length}週の件数</SubHead>
      <div className="overflow-x-auto px-4 py-3">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-2xs text-faint">
              <th className="py-1 pr-3 font-medium" />
              <th className="py-1 pr-3 font-medium">合計</th>
              {s.weeks.map((w) => (
                <th key={w} className="tnum py-1 pr-3 font-medium">
                  {weekLabel(w)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="border-t border-hairline font-medium">
              <td className="py-1.5 pr-3">頼まれた</td>
              <td className="tnum py-1.5 pr-3">{s.total.asked}</td>
              {s.byWeek.map((w, i) => (
                <td key={s.weeks[i]} className="tnum py-1.5 pr-3">
                  {w.asked}
                </td>
              ))}
            </tr>
            <tr className="border-t border-hairline">
              <td className="py-1.5 pr-3">頼んだ人数</td>
              <td className="tnum py-1.5 pr-3">{s.total.people}</td>
              {s.byWeek.map((w, i) => (
                <td key={s.weeks[i]} className="tnum py-1.5 pr-3 text-muted">
                  {w.people}
                </td>
              ))}
            </tr>
            {s.categories.map((c) => (
              <tr key={c.id} className="border-t border-hairline">
                <td className="py-1.5 pl-3 pr-3 text-muted">
                  {CATEGORY[c.id] ?? c.id}
                </td>
                <td className="tnum py-1.5 pr-3">{c.asked}</td>
                {c.byWeek.map((n, i) => (
                  <td key={s.weeks[i]} className="tnum py-1.5 pr-3 text-muted">
                    {n === 0 ? "—" : n}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-2xs leading-relaxed text-faint">{NOTE}</p>
      </div>
      {s.people.length > 0 && (
        <>
          <SubHead>頼んだ人</SubHead>
          <ul>
            {s.people.map((p) => (
              <li
                key={p.name}
                className="flex items-center justify-between gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
              >
                <span className="truncate">{p.name}</span>
                <span className="tnum shrink-0 text-muted">{p.asked}件</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

/** 概要ページのカード。「使われているか」を件数と人数で見せる。 */
export function RequestCard() {
  const q = useFetch<Log>("/data/requests");
  const s = summarize(q.data);
  if (s === null) return null;
  const talkOnly = s.total.asked - s.total.acted;
  return (
    <Card
      title="頼まれているか"
      desc={`直近${s.weeks.length}週に、人から頼まれた回数です`}
      right={
        <Link to="/data#requests" className="text-2xs text-accent-deep">
          内訳を見る
        </Link>
      }
    >
      <div className="grid grid-cols-2 gap-4 p-4">
        <Metric
          label="頼まれた"
          value={s.total.asked}
          sub={`今週 ${s.byWeek[0]?.asked ?? 0}・会話だけで返した ${talkOnly}`}
          tone={s.total.asked > 0 ? "accent" : "warn"}
        />
        <Metric
          label="頼んだ人"
          value={`${s.total.people}人`}
          sub={s.people.map((p) => `${p.name} ${p.asked}`).join("・")}
        />
      </div>
      {s.categories.length > 0 && (
        <ul>
          {s.categories.slice(0, 5).map((c) => (
            <li
              key={c.id}
              className="flex items-center justify-between gap-3 border-t border-hairline px-4 py-2 text-xs"
            >
              <span className="truncate">{CATEGORY[c.id] ?? c.id}</span>
              <span className="tnum shrink-0 text-muted">{c.asked}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
