import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { Avatar } from "../components/Avatar.tsx";
import { SettingRow, type SaveFn } from "../components/SettingRow.tsx";
import { Card, Chip, Empty, ErrorNote, Loading, Metric } from "../components/ui.tsx";
import { api, useFetch } from "../lib/api.ts";
import { jstStamp } from "../lib/format.ts";
import type { AgentDetail, ResolvedSetting } from "../lib/types.ts";
import { DevBotPanels } from "./DevBotPanels.tsx";

export function AgentPage({ onChanged }: { onChanged: () => void }) {
  const { id = "" } = useParams();
  const { data, error, reload } = useFetch<AgentDetail>(`/agents/${id}`);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [filter, setFilter] = useState("");

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
  const needle = filter.trim().toLowerCase();
  const groups =
    needle === ""
      ? data.groups
      : data.groups
          .map((g) => ({ ...g, settings: g.settings.filter((x) => matches(x, needle)) }))
          .filter((g) => g.settings.length > 0);

  return (
    <div className="space-y-6">
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
                label="本日の枠"
                value={
                  <>
                    {s.quota.used}
                    <span className="text-sm font-normal text-faint">/{s.quota.limit}</span>
                  </>
                }
                sub={s.quota.source === "config" ? "設定どおり" : "会話で変更されています"}
                tone="accent"
              />
              <Metric label="最終観察" value={jstStamp(s.lastRunAt)} sub="観察ループの前回実行" />
              <Metric
                label="自発ループ"
                value={
                  <>
                    {s.cycleCount.enabled}
                    <span className="text-sm font-normal text-faint">/{s.cycleCount.total}</span>
                  </>
                }
                sub="有効な件数"
              />
            </>
          )}
        </div>
      </div>

      {saveError !== null && <ErrorNote message={saveError} />}
      {saveError === null && savedAt !== null && (
        <div className="rounded-md border border-accent/25 bg-accent-soft px-3 py-2 text-xs text-accent-deep">
          保存しました。上部の「適用」を押すとBOTに反映されます。
        </div>
      )}

      {data.service === "devbot" && <DevBotPanels />}

      <SettingFilter value={filter} onChange={setFilter} total={countSettings(data.groups)} />

      {groups.length === 0 ? (
        <Card>
          <Empty>「{filter}」に一致する設定はありません</Empty>
        </Card>
      ) : (
        groups.map((g) => (
          <Card key={g.id} title={g.label} desc={g.desc} right={<GroupCount group={g} />}>
            {g.settings.length === 0 ? (
              <Empty>設定はありません</Empty>
            ) : (
              <div>
                {g.settings.map((setting) => (
                  <SettingRow
                    key={setting.path}
                    setting={setting}
                    onSave={save}
                    idNames={data.idNames ?? {}}
                  />
                ))}
              </div>
            )}
          </Card>
        ))
      )}
    </div>
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

function GroupCount({ group }: { group: AgentDetail["groups"][number] }) {
  const toggleable = group.settings.filter((s) => s.kind === "bool" || s.kind === "tri");
  if (toggleable.length === 0) return null;
  const on = toggleable.filter((s) =>
    s.kind === "tri" ? s.current.value !== "off" : s.current.value === true,
  ).length;
  return (
    <Chip tone={on > 0 ? "accent" : "neutral"}>
      {on} / {toggleable.length} 有効
    </Chip>
  );
}
