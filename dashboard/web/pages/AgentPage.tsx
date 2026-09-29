import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Avatar } from "../components/Avatar.tsx";
import { type SaveFn } from "../components/SettingRow.tsx";
import { SettingsList } from "../components/SettingsList.tsx";
import {
  Card,
  Chip,
  Empty,
  ErrorNote,
  ListState,
  Loading,
  Metric,
  Tips,
  Tabs,
} from "../components/ui.tsx";
import { api, useFetch } from "../lib/api.ts";
import { CYCLE_CATEGORIES } from "../lib/categories.ts";
import {
  actionLabel,
  jstStamp,
  kindLabel,
  plainDiscord,
} from "../lib/format.ts";
import type {
  ActivityRow,
  AgentDetail,
  ResolvedSetting,
} from "../lib/types.ts";
import { DevBotPanels } from "./DevBotPanels.tsx";

export function AgentPage({ onChanged }: { onChanged: () => void }) {
  const { id = "" } = useParams();
  const { data, error, reload } = useFetch<AgentDetail>(`/agents/${id}`);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [filter, setFilter] = useState("");
  const [tab, setTab] = useState<AgentTab>("basic");

  // 保存の合図は出しっぱなしにしない（どの保存の結果か分からなくなるため）
  useEffect(() => {
    if (savedAt === null) return;
    const t = setTimeout(() => setSavedAt(null), 6000);
    return () => clearTimeout(t);
  }, [savedAt]);

  const save: SaveFn = useCallback(
    async (path, value) => {
      setSaveError(null);
      const scope = id === "devbot" ? "global" : `agent:${id}`;
      try {
        await api.patch("/config", { scope, changes: [{ path, value }] });
        setSavedAt(Date.now());
        reload();
        onChanged();
      } catch (e) {
        setSaveError((e as Error).message);
        throw e;
      }
    },
    [id, reload, onChanged],
  );

  if (error !== null) return <ErrorNote message={error} onRetry={reload} />;
  if (data === null) return <Loading rows={6} />;

  const s = data.summary;
  const idNames = data.idNames ?? {};
  const isDevBot = data.service === "devbot";
  const needle = filter.trim().toLowerCase();
  const allGroups =
    needle === ""
      ? data.groups
      : data.groups
          .map((g) => ({
            ...g,
            settings: g.settings.filter((x) => matches(x, needle)),
          }))
          .filter((g) => g.settings.length > 0);
  const group = (id: string) =>
    data.groups.find((g) => g.id === id)?.settings ?? [];
  const basic = data.groups
    .flatMap((g) => g.settings)
    .filter((x) => x.level === "basic");
  const cycles = group("proactive-cycles");

  const header = (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="flex items-center gap-4">
        <Avatar id={data.id} name={data.name} size="lg" />
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{data.name}</h1>
          <p className="mt-1 max-w-[70ch] text-xs leading-relaxed text-muted">
            {s?.role === "" || s?.role === undefined ? "全般担当" : s.role}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-6">
        {s !== null && data.service === "archivebot" && (
          <>
            <Metric
              label="本日の自発発言"
              value={
                <>
                  {s.quota.used}
                  <span className="text-sm font-normal text-faint">
                    /{s.quota.limit}
                  </span>
                </>
              }
              sub={
                s.quota.source === "config"
                  ? "1日の上限（設定どおり）"
                  : "上限を会話で変更中"
              }
              tone="accent"
            />
            <Metric
              label="最後に見回った時刻"
              value={jstStamp(s.lastRunAt)}
              sub="「観察の間隔」の設定ごとに会話を見回ります"
            />
            <Metric
              label="自分から動く機能"
              value={
                <>
                  {s.cycleCount.enabled}
                  <span className="text-sm font-normal text-faint">
                    /{s.cycleCount.total}
                  </span>
                </>
              }
              sub="ONになっている数"
            />
          </>
        )}
      </div>
    </div>
  );

  const notices = (
    <>
      {saveError !== null && <ErrorNote message={saveError} />}
      {saveError === null && savedAt !== null && (
        <div className="rounded-md border border-accent/25 bg-accent-soft px-3 py-2 text-xs text-accent-deep">
          保存しました。画面上部の「適用」を押すとBOTに反映されます。
        </div>
      )}
    </>
  );

  // 開発BOTは設定が少ないので、タブは作らず固有パネル＋設定1枚にする
  if (isDevBot) {
    return (
      <div className="space-y-6">
        {header}
        {notices}
        <DevBotPanels />
        <Card
          title="開発BOTの設定"
          desc="別プロセスで動くため、ここの変更は開発BOTだけを再起動します。"
        >
          <SettingsList
            settings={data.groups.flatMap((g) => g.settings)}
            onSave={save}
            idNames={idNames}
          />
        </Card>
      </div>
    );
  }

  const tabs: { id: AgentTab; label: string; count?: number }[] = [
    { id: "basic", label: "よく使う設定", count: basic.length },
    { id: "cycles", label: "自分から動く機能", count: cycles.length },
    { id: "skills", label: "スキル", count: group("skills").length },
    { id: "all", label: "すべての設定", count: countSettings(data.groups) },
  ];

  return (
    <div className="space-y-6">
      {header}
      <Tips
        tipsKey="agent"
        tips={[
          "普段の運用は「よく使う設定」だけで足ります。迷ったらここから",
          "「シャドー」は、実際には投稿せず記録だけ残す試運転です。新しい機能はシャドーで様子を見てから「本番」にします",
          "変えたら画面上部の「適用」を押すと反映されます（押すまではBOTの動きは変わりません）",
        ]}
      />
      <RecentMoves agentId={data.id} />
      {notices}
      <Tabs tabs={tabs} value={tab} onChange={setTab} />

      {tab === "basic" && (
        <Card
          title="よく使う設定"
          desc="呼ばれ方・1日に自分から話す回数・主な機能のON/OFF。まずはここだけ見れば大丈夫です"
        >
          <SettingsList
            settings={basic}
            onSave={save}
            idNames={idNames}
            showAll
          />
        </Card>
      )}

      {tab === "cycles" && (
        <div className="space-y-6">
          <Card
            title="全体のスイッチ"
            desc="自分から動く機能ぜんぶに効く設定。ここがOFFだと下の機能はすべて止まります"
          >
            <SettingsList
              settings={group("proactive-common")}
              onSave={save}
              idNames={idNames}
            />
          </Card>
          {CYCLE_CATEGORIES.map((c) => {
            const items = cycles.filter((x) => x.category === c.id);
            if (items.length === 0) return null;
            return (
              <Card
                key={c.id}
                title={c.label}
                desc={c.desc}
                right={<GroupCount settings={items} />}
              >
                <SettingsList
                  settings={items}
                  onSave={save}
                  idNames={idNames}
                  showAll
                />
              </Card>
            );
          })}
        </div>
      )}

      {tab === "skills" && (
        <Card title="スキル" desc="呼ばれたとき／条件を満たしたときに働く能力">
          <SettingsList
            settings={group("skills")}
            onSave={save}
            idNames={idNames}
            showAll
          />
        </Card>
      )}

      {tab === "all" && (
        <>
          <SettingFilter
            value={filter}
            onChange={setFilter}
            total={countSettings(data.groups)}
          />
          {allGroups.length === 0 ? (
            <Card>
              <Empty>「{filter}」に一致する設定はありません</Empty>
            </Card>
          ) : (
            allGroups.map((g) => (
              <Card
                key={g.id}
                title={g.label}
                desc={g.desc}
                right={<GroupCount settings={g.settings} />}
              >
                <SettingsList
                  settings={g.settings}
                  onSave={save}
                  idNames={idNames}
                  showAll
                />
              </Card>
            ))
          )}
        </>
      )}
    </div>
  );
}

type AgentTab = "basic" | "cycles" | "skills" | "all";

/** この子の最近の自発行動（3件）。設定の前に「何をしている子か」を見せる。 */
function RecentMoves({ agentId }: { agentId: string }) {
  const q = useFetch<ActivityRow[]>("/activity?limit=200");
  const rows = (q.data ?? []).filter((r) => r.agentId === agentId).slice(0, 3);
  return (
    <Card
      title="最近の動き"
      desc="呼ばれていないのに自分から動いた記録（新しい順）"
      right={
        <Link
          to="/"
          className="focus-ring rounded text-2xs text-muted underline decoration-dotted hover:text-ink"
        >
          概要でもっと見る
        </Link>
      }
    >
      {rows.length === 0 ? (
        <ListState q={q} empty="最近の自発行動はありません" />
      ) : (
        <ul>
          {rows.map((r) => (
            <li
              key={r.id}
              className="flex items-baseline gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
            >
              <span className="tnum w-24 shrink-0 text-2xs text-faint">
                {jstStamp(r.createdAt)}
              </span>
              <span className="shrink-0 text-muted">{kindLabel(r.kind)}</span>
              <Chip>{actionLabel(r.action)}</Chip>
              <span className="min-w-0 flex-1 truncate text-faint">
                {plainDiscord(r.postedExcerpt ?? r.detail)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/** ラベル（子設定を含む）に検索語を含む設定だけを残す。 */
function matches(s: ResolvedSetting, needle: string): boolean {
  if (s.label.toLowerCase().includes(needle)) return true;
  if ((s.desc ?? "").toLowerCase().includes(needle)) return true;
  return (s.children ?? []).some((c) => matches(c, needle));
}

function countSettings(groups: AgentDetail["groups"]): number {
  return groups.reduce((n, g) => n + g.settings.length, 0);
}

/**
 * 設定の絞り込み。1エージェントに60件以上並ぶので、
 * 目的の設定を総当たりで探させない。
 */
function SettingFilter({
  value,
  onChange,
  total,
}: {
  value: string;
  onChange: (v: string) => void;
  total: number;
}) {
  return (
    <div className="flex items-center gap-2">
      <input
        className="input max-w-[320px]"
        type="search"
        value={value}
        placeholder={`設定を絞り込む（全${total}件）`}
        aria-label="設定を絞り込む"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") onChange("");
        }}
      />
      {value !== "" && (
        <button
          type="button"
          className="focus-ring rounded-md border border-hairline px-2.5 py-1.5 text-2xs text-muted hover:bg-canvas"
          onClick={() => onChange("")}
        >
          絞り込みを解除
        </button>
      )}
    </div>
  );
}

function GroupCount({ settings }: { settings: ResolvedSetting[] }) {
  const toggleable = settings.filter(
    (s) => s.kind === "bool" || s.kind === "tri",
  );
  if (toggleable.length === 0) return null;
  const on = toggleable.filter((s) =>
    s.kind === "tri" ? s.current.value !== "off" : s.current.value === true,
  ).length;
  return (
    <Chip tone={on > 0 ? "accent" : "neutral"}>
      {on} / {toggleable.length} ON
    </Chip>
  );
}
