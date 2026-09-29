import { useEffect, useRef, useState } from "react";

import {
  Async,
  Button,
  Card,
  Chip,
  Empty,
  Loading,
  PageHeader,
  StatusDot,
} from "../components/ui.tsx";
import { api, useFetch } from "../lib/api.ts";
import {
  agentLabel,
  bytes,
  jstStamp,
  relTime,
  since,
  STATUS_TONE,
} from "../lib/format.ts";
import { isStale, loopInfo } from "../lib/loops.ts";
import type { LogLine, RestartResult, ServiceStatus } from "../lib/types.ts";

type LogInventory = {
  thresholdBytes: number;
  note: string;
  items: {
    id: string;
    label: string;
    path: string;
    sizeBytes: number | null;
    rotated: boolean;
  }[];
};

const LEVEL_STYLE: Record<LogLine["level"], string> = {
  error: "text-danger",
  warn: "text-warn",
  info: "text-ink",
  debug: "text-faint",
};

function LogViewer({
  target,
  setTarget,
  inventory,
  inventoryError,
}: {
  target: string;
  setTarget: (t: string) => void;
  inventory: LogInventory | null;
  inventoryError: string | null;
}) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [hideNoise, setHideNoise] = useState(true);
  const [follow, setFollow] = useState(true);
  const [connected, setConnected] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    const es = new EventSource(
      `/api/logs/${encodeURIComponent(target)}/stream`,
    );
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("init", (ev) => {
      const payload = JSON.parse((ev as MessageEvent<string>).data) as {
        lines: LogLine[];
      };
      setLines(payload.lines);
    });
    es.addEventListener("lines", (ev) => {
      const fresh = JSON.parse((ev as MessageEvent<string>).data) as LogLine[];
      setLines((prev) => [...prev, ...fresh].slice(-2000));
    });
    return () => es.close();
  }, [target]);

  useEffect(() => {
    if (follow && boxRef.current !== null) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [lines, follow]);

  const shown = hideNoise ? lines.filter((l) => !l.noisy) : lines;

  return (
    <Card
      title="ログ"
      desc="ファイルの末尾を追いかけて表示します（ローテーションにも追従）"
      right={
        <span className="flex items-center gap-2">
          <span
            className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-accent" : "bg-faint"}`}
            title={connected ? "追従中" : "接続待ち"}
          />
          <select
            className="input max-w-[280px] py-1 text-2xs"
            value={target}
            aria-label="表示するログ"
            disabled={inventory === null}
            onChange={(e) => setTarget(e.target.value)}
          >
            {inventory === null && (
              <option value={target}>
                {inventoryError === null
                  ? "読み込んでいます…"
                  : "一覧を取得できませんでした"}
              </option>
            )}
            {inventory?.items.map((i) => (
              <option key={i.id} value={i.id}>
                {i.label}
              </option>
            ))}
          </select>
        </span>
      }
    >
      <div className="flex items-center gap-4 border-b border-hairline px-4 py-2 text-2xs text-muted">
        <label className="flex cursor-pointer items-center gap-1.5">
          <input
            type="checkbox"
            checked={hideNoise}
            onChange={(e) => setHideNoise(e.target.checked)}
          />
          定型の警告を隠す（PyNaCl など）
        </label>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
          />
          自動スクロール
        </label>
        <span className="tnum ml-auto text-faint">{shown.length} 行</span>
      </div>
      <div
        ref={boxRef}
        style={{ height: "min(58vh, 620px)" }}
        className="overflow-auto bg-[#FCFCFA] px-4 py-2 font-mono text-[11px] leading-[1.7]"
      >
        {shown.length === 0 ? (
          <Empty>行がありません</Empty>
        ) : (
          shown.map((l) => (
            <div
              key={`${l.seq}-${l.text.slice(0, 24)}`}
              className={`whitespace-pre-wrap break-all ${LEVEL_STYLE[l.level]} ${
                l.boundary
                  ? "my-1 border-t border-dashed border-hairline pt-1 font-semibold"
                  : ""
              }`}
            >
              {l.timestamp !== null && (
                <span className="text-faint">{l.timestamp} </span>
              )}
              {l.text}
            </div>
          ))
        )}
      </div>
    </Card>
  );
}

function ServiceRow({ service }: { service: ServiceStatus }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  const [confirming, setConfirming] = useState(false);

  // Esc で確認を取り消す（押してしまった時の逃げ道）
  useEffect(() => {
    if (!confirming) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setConfirming(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming]);

  const restart = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.post<RestartResult>(
        `/ops/services/${service.id}/restart`,
      );
      setResult({ ok: r.ok, text: r.detail });
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="border-t border-hairline px-4 py-3 first:border-t-0">
      <div className="flex flex-wrap items-center gap-3">
        <StatusDot status={service.status} pulse />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{service.label}</span>
            {!service.enabled && <span className="chip shrink-0">オフ</span>}
          </div>
          <div className="mt-0.5 text-2xs text-muted">{service.detail}</div>
        </div>

        <div className="tnum flex shrink-0 items-center gap-4 text-2xs text-faint">
          {service.pid !== null && <span>pid {service.pid}</span>}
          {service.uptimeSec !== null && (
            <span title="この起動からの経過時間">稼働 {relTime(service.uptimeSec)}</span>
          )}
          {service.restarts > 0 && (
            <span title="常駐プロセスが再起動した回数。増え続けるならクラッシュループ">
              再起動 {service.restarts}回
            </span>
          )}
          {service.logAgeSec !== null && <span>ログ {relTime(service.logAgeSec)}</span>}
        </div>

        <span className={`chip shrink-0 ${STATUS_TONE[service.status].chip}`}>
          {service.statusLabel}
        </span>

        {service.enabled &&
          (confirming ? (
            <span className="flex shrink-0 items-center gap-1.5">
              <Button
                variant="danger"
                busy={busy}
                onClick={() => void restart()}
              >
                本当に再起動
              </Button>
              <Button onClick={() => setConfirming(false)}>やめる</Button>
            </span>
          ) : (
            <Button onClick={() => setConfirming(true)}>再起動</Button>
          ))}
      </div>

      {service.note !== undefined && (
        <p className="mt-1.5 pl-5 text-2xs text-faint">※ {service.note}</p>
      )}
      {service.heartbeatDetail !== null && (
        <p className="mt-1 pl-5 text-2xs text-faint">
          {service.heartbeatDetail}
        </p>
      )}
      {result !== null && (
        <p
          className={`mt-2 rounded-md px-2.5 py-1.5 text-2xs ${
            result.ok
              ? "bg-accent-soft text-accent-deep"
              : "bg-danger-soft text-danger"
          }`}
        >
          {result.text}
        </p>
      )}
    </div>
  );
}

type SubLoop = { loop: string; scope: string; lastRunAt: string | null };
type SubLoopView = { items: SubLoop[]; idNames: Record<string, string> };
type LoopGroupData = {
  loop: string;
  rows: SubLoop[];
  lastRunAt: string | null;
};

/** 同じループの行をまとめる。attention は1チャンネル1行で数十行になるため。 */
function groupLoops(items: SubLoop[]): LoopGroupData[] {
  const map = new Map<string, SubLoop[]>();
  for (const it of items) {
    const arr = map.get(it.loop) ?? [];
    arr.push(it);
    map.set(it.loop, arr);
  }
  return [...map.entries()]
    .map(([loop, rows]) => ({
      loop,
      rows: [...rows].sort((a, b) =>
        (b.lastRunAt ?? "").localeCompare(a.lastRunAt ?? ""),
      ),
      lastRunAt: rows.reduce<string | null>(
        (max, r) => ((r.lastRunAt ?? "") > (max ?? "") ? r.lastRunAt : max),
        null,
      ),
    }))
    .sort((a, b) => (b.lastRunAt ?? "").localeCompare(a.lastRunAt ?? ""));
}

/** 1つのループ。名前と説明を出し、対象が多いものは押したときだけ内訳を出す。 */
function LoopGroup({
  group,
  idNames,
}: {
  group: LoopGroupData;
  idNames: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const info = loopInfo(group.loop);
  const many = group.rows.length > 1;
  const label = (scope: string): string =>
    (idNames ?? {})[scope] ?? agentLabel(scope);

  return (
    <li className="border-t border-hairline first:border-t-0">
      <button
        type="button"
        aria-expanded={many ? open : undefined}
        disabled={!many}
        onClick={() => setOpen((o) => !o)}
        className="focus-ring flex w-full items-start gap-3 px-4 py-2 text-left text-xs disabled:cursor-default"
      >
        <span
          className={`mt-0.5 w-3 text-2xs text-muted transition-transform ${open ? "rotate-90" : ""}`}
        >
          {many ? "▸" : ""}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="font-medium">{info.label}</span>
            {many ? (
              <Chip>{group.rows.length}件</Chip>
            ) : (
              <span className="truncate text-2xs text-faint">
                {label(group.rows[0]?.scope ?? "")}
              </span>
            )}
          </span>
          <span className="mt-0.5 block text-2xs text-muted">{info.desc}</span>
        </span>
        <span className="tnum shrink-0 text-2xs text-muted">
          {jstStamp(group.lastRunAt)}
        </span>
      </button>

      {open && (
        <ul className="bg-canvas/60 pb-1">
          {group.rows.map((r) => (
            <li
              key={r.scope}
              className="flex items-center gap-3 px-4 py-1 pl-10 text-2xs"
            >
              <span className="min-w-0 flex-1 truncate text-muted">
                {label(r.scope)}
              </span>
              <span className="tnum shrink-0 text-muted">
                {jstStamp(r.lastRunAt)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function SubLoops({ view }: { view: SubLoopView }) {
  const [showStale, setShowStale] = useState(false);
  const groups = groupLoops(view.items ?? []);
  const live = groups.filter((g) => !isStale(g.loop, g.lastRunAt));
  const stale = groups.filter((g) => isStale(g.loop, g.lastRunAt));
  return (
    <>
      <ul>
        {live.map((g) => (
          <LoopGroup key={g.loop} group={g} idNames={view.idNames ?? {}} />
        ))}
      </ul>
      {stale.length > 0 && (
        <div className="border-t border-hairline">
          <button
            type="button"
            aria-expanded={showStale}
            onClick={() => setShowStale((v) => !v)}
            className="focus-ring flex w-full items-center gap-2 px-4 py-2 text-left text-2xs text-muted hover:text-ink"
          >
            <span
              className={`transition-transform ${showStale ? "rotate-90" : ""}`}
            >
              ▸
            </span>
            しばらく動いていない機能 {stale.length}
            件（廃止・停止したもの、または周期より長く止まっているもの）
          </button>
          {showStale && (
            <ul className="opacity-70">
              {stale.map((g) => (
                <LoopGroup
                  key={g.loop}
                  group={g}
                  idNames={view.idNames ?? {}}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

export function OpsPage({ services }: { services: ServiceStatus[] }) {
  const inventoryQ = useFetch<LogInventory>("/ops/logs");
  const subloopsQ = useFetch<SubLoopView>("/ops/subloops");
  const inventory = inventoryQ.data;
  const [target, setTarget] = useState("archivebot:out");
  const logRef = useRef<HTMLDivElement>(null);

  /** ログファイルを押したら、下のログ表示をそのファイルに切り替えてそこへ移動する。 */
  const openLog = (id: string) => {
    setTarget(id);
    logRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="運用"
        lead="BOTが動いているか・止まっていないかを見るページです。落ちたBOTは自動で再起動されます。"
        tipsKey="ops"
        tips={[
          "上の一覧で「稼働中」以外の色になっていたら要注意です。まず「再起動」を試してください（会話エージェントは全員が同じプロセスなので、まとめて再起動されます）",
          "ログファイルの名前を押すと、一番下のログ表示がそのファイルに切り替わります",
          "サブループは、各機能が最後に動いた時刻です。止まっている機能は下に畳んであります",
        ]}
      />

      <Card
        title="常駐プロセス"
        desc="常駐しているBOT。落ちたら自動で再起動します"
      >
        {services.length === 0 ? (
          <Loading rows={4} />
        ) : (
          services.map((s) => <ServiceRow key={s.id} service={s} />)
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        <Card
          title="機能ごとの最終実行"
          desc="自分から動く機能（サブループ）が最後に動いた時刻。対象が多いものは押すと内訳が出ます"
        >
          <Async q={subloopsQ} empty="記録がありません" rows={6}>
            {(v) => <SubLoops view={v} />}
          </Async>
        </Card>

        <Card title="ログファイル" desc={inventory?.note}>
          <Async q={inventoryQ} empty="ログファイルがありません" rows={5}>
            {(inv) => (
              <ul>
                {inv.items.map((i) => {
                  const over =
                    (i.sizeBytes ?? 0) > (inv.thresholdBytes ?? Infinity);
                  const active = i.id === target;
                  return (
                    <li
                      key={i.id}
                      className="border-t border-hairline first:border-t-0"
                    >
                      <button
                        type="button"
                        onClick={() => openLog(i.id)}
                        aria-current={active ? "true" : undefined}
                        className={`focus-ring flex w-full items-center gap-3 px-4 py-2 text-left text-xs hover:bg-canvas ${
                          active ? "bg-accent-soft/60" : ""
                        }`}
                      >
                        <span
                          className={`min-w-0 flex-1 truncate ${active ? "font-medium text-accent-deep" : "underline decoration-dotted"}`}
                        >
                          {i.label}
                        </span>
                        {active && <Chip tone="accent">表示中</Chip>}
                        {!i.rotated && <Chip tone="warn">自動退避なし</Chip>}
                        <span
                          className={`tnum shrink-0 ${over ? "text-warn" : "text-faint"}`}
                        >
                          {bytes(i.sizeBytes)}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </Async>
        </Card>
      </div>

      <div ref={logRef} className="scroll-mt-4">
        <LogViewer
          target={target}
          setTarget={setTarget}
          inventory={inventory}
          inventoryError={inventoryQ.error}
        />
      </div>
    </div>
  );
}
