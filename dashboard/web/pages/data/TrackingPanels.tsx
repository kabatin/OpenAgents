import { Chip, ListState } from "../../components/ui.tsx";
import { useFetch } from "../../lib/api.ts";
import { ja, jstDate, jstStamp } from "../../lib/format.ts";
import { IdCell, PanelHead, Row } from "./common.tsx";
import type { Reminder, TaskRow } from "./types.ts";

/** 状態の内部コードを人の言葉に（種類ごとに意味が違う）。 */
const STAGE_LABEL: Record<string, string> = {
  open: "見守り中",
  asked: "声かけ済み",
  asked2: "2回目の声かけ済み",
  once: "1回だけ",
  daily: "毎日",
  weekly: "毎週",
  monthly: "毎月",
  monthly_end: "毎月末",
  none: "通知前",
  before: "2日前に通知済み",
  day: "当日に通知済み",
  overdue: "期日超過を通知済み",
  stale: "手放し済み",
};

function stageLabel(stage: string | null): string {
  return stage === null ? "" : (STAGE_LABEL[stage] ?? ja(stage));
}

/** 担当者欄の <@ID> を名前に（引けなければ「(不明な人)」）。 */
function ownerLabel(
  owner: string | null,
  idNames: Record<string, string>,
): string {
  return (owner ?? "").replace(
    /<@!?(\d{17,20})>/g,
    (_, id: string) => idNames[id] ?? "(不明な人)",
  );
}

const TASK_KIND: Record<TaskRow["kind"], string> = {
  action: "議事録",
  homework: "宿題",
  reminder: "リマインド",
};

/** 追いかけていること①: 期日のあるタスク。 */
export function TasksPanel() {
  const q = useFetch<{ items: TaskRow[]; idNames?: Record<string, string> }>(
    "/data/tasks",
  );
  const idNames = q.data?.idNames ?? {};
  const items = q.data?.items ?? [];
  return (
    <>
      <PanelHead
        title="追跡タスク"
        what="議事録のTODO（A）・「やっときます」の宿題（H）・リマインダー（R）をまとめて追いかけています"
        how="Discordで「H27 を完了に」「A6 を 9/12 に」のように番号で話しかけます"
      />
      <ul className="overflow-x-auto">
        {items.length === 0 ? (
          <ListState
            q={q}
            empty="追跡中のタスクはありません。議事録に期日つきのTODOが載ると自動で追跡します"
          />
        ) : (
          items.map((t) => (
            <Row key={t.key}>
              <span className="tnum w-10 shrink-0 font-medium text-accent-deep">
                {t.key}
              </span>
              <span className="w-20 shrink-0 whitespace-nowrap">
                <Chip tone={t.status === "stale" ? "warn" : "neutral"}>
                  {TASK_KIND[t.kind]}
                </Chip>
              </span>
              <span className="tnum w-24 shrink-0 text-muted">
                {t.due === null ? "期日なし" : jstDate(t.due)}
              </span>
              <span className="min-w-0 flex-1 break-all">{t.task}</span>
              {t.owner && (
                <span className="w-28 shrink-0 truncate text-right text-faint">
                  {ownerLabel(t.owner, idNames)}
                </span>
              )}
              <span className="w-28 shrink-0 text-right text-2xs text-muted">
                {stageLabel(t.stage)}
              </span>
            </Row>
          ))
        )}
      </ul>
    </>
  );
}

/** 追いかけていること②: 人が頼んだリマインダー。 */
export function RemindersPanel() {
  const q = useFetch<{ items: Reminder[] }>("/data/reminders");
  const active = (q.data?.items ?? []).filter((r) => r.status === "active");
  return (
    <>
      <PanelHead
        title="リマインダー"
        what="人から頼まれて登録した、時刻になったら知らせる予定（動いているものだけ）"
        how="Discordで「明日10時に〜って教えて」「毎週金曜に〜」と頼みます。止めるときもDiscordで頼みます"
      />
      <ul>
        {active.length === 0 ? (
          <ListState q={q} empty="動いているリマインダーはありません" />
        ) : (
          active.map((r) => (
            <Row key={r.id}>
              <IdCell id={r.id} />
              <span className="tnum w-28 shrink-0 font-medium">
                {jstStamp(r.due)}
              </span>
              {r.repeat !== "once" && <Chip tone="info">{r.repeat}</Chip>}
              <span className="min-w-0 flex-1 truncate">{r.content}</span>
              <span className="shrink-0 text-2xs text-faint">
                {r.channel_label ?? ""} / {r.user_name}
              </span>
            </Row>
          ))
        )}
      </ul>
    </>
  );
}
