import { useEffect, useRef, useState } from "react";

import { Button, Card, Chip, Empty, StatusDot } from "../components/ui.tsx";
import { api, useFetch } from "../lib/api.ts";
import { bytes, relTime, since, STATUS_TONE } from "../lib/format.ts";
import type { LogLine, RestartResult, ServiceStatus } from "../lib/types.ts";

type LogInventory = {
  thresholdBytes: number;
  note: string;
  items: { id: string; label: string; path: string; sizeBytes: number | null; rotated: boolean }[];
};

const LEVEL_STYLE: Record<LogLine["level"], string> = {
  error: "text-danger",
  warn: "text-warn",
  info: "text-ink",
  debug: "text-faint",
};

function LogViewer() {
  const { data: inventory } = useFetch<LogInventory>("/ops/logs");
  const [target, setTarget] = useState("archivebot:out");
  const [lines, setLines] = useState<LogLine[]>([]);
  const [hideNoise, setHideNoise] = useState(true);
  const [follow, setFollow] = useState(true);
  const [connected, setConnected] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    const es = new EventSource(`/api/logs/${encodeURIComponent(target)}/stream`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("init", (ev) => {
      const payload = JSON.parse((ev as MessageEvent<string>).data) as { lines: LogLine[] };
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
            onChange={(e) => setTarget(e.target.value)}
          >
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
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          自動スクロール
        </label>
        <span className="tnum ml-auto text-faint">{shown.length} 行</span>
      </div>
      <div
        ref={boxRef}
        className="h-[420px] overflow-auto bg-[#FCFCFA] px-4 py-2 font-mono text-[11px] leading-[1.7]"
      >
        {shown.length === 0 ? (
          <Empty>行がありません</Empty>
        ) : (
          shown.map((l) => (
            <div
              key={`${l.seq}-${l.text.slice(0, 24)}`}
              className={`whitespace-pre-wrap break-all ${LEVEL_STYLE[l.level]} ${
                l.boundary ? "my-1 border-t border-dashed border-hairline pt-1 font-semibold" : ""
              }`}
            >
              {l.timestamp !== null && <span className="text-faint">{l.timestamp} </span>}
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
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirming, setConfirming] = useState(false);

  const restart = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.post<RestartResult>(`/ops/services/${service.id}/restart`);
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
              <Button variant="danger" busy={busy} onClick={() => void restart()}>
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
        <p className="mt-1 pl-5 text-2xs text-faint">{service.heartbeatDetail}</p>
      )}
      {result !== null && (
        <p
          className={`mt-2 rounded-md px-2.5 py-1.5 text-2xs ${
            result.ok ? "bg-accent-soft text-accent-deep" : "bg-danger-soft text-danger"
          }`}
        >
          {result.text}
        </p>
      )}
    </div>
  );
}

export function OpsPage({ services }: { services: ServiceStatus[] }) {
  const { data: inventory } = useFetch<LogInventory>("/ops/logs");
  const { data: subloops } = useFetch<{ loop: string; scope: string; lastRunAt: string | null }[]>(
    "/ops/subloops",
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">運用</h1>
        <p className="mt-1 max-w-[70ch] text-xs leading-relaxed text-muted">
          常駐プロセスの稼働状況とログ。落ちたBOTは自動で再起動されます。
        </p>
      </div>

      <Card title="常駐プロセス">
        {services.length === 0 ? (
          <Empty>読み込んでいます…</Empty>
        ) : (
          services.map((s) => <ServiceRow key={s.id} service={s} />)
        )}
      </Card>

      <LogViewer />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="ログファイル" desc={inventory?.note}>
          <ul>
            {inventory?.items.map((i) => {
              const over = (i.sizeBytes ?? 0) > (inventory.thresholdBytes ?? Infinity);
              return (
                <li
                  key={i.id}
                  className="flex items-center gap-3 border-t border-hairline px-4 py-2 text-xs first:border-t-0"
                >
                  <span className="min-w-0 flex-1 truncate">{i.label}</span>
                  {!i.rotated && <Chip tone="warn">自動退避なし</Chip>}
                  <span className={`tnum shrink-0 ${over ? "text-warn" : "text-faint"}`}>
                    {bytes(i.sizeBytes)}
                  </span>
                </li>
              );
            })}
          </ul>
        </Card>

        <Card title="サブループの最終実行" desc="観察ループ以外の細かい進行状況（チェックポイント）">
          {(subloops?.length ?? 0) === 0 ? (
            <Empty>記録がありません</Empty>
          ) : (
            <ul>
              {subloops?.map((s) => (
                <li
                  key={`${s.loop}:${s.scope}`}
                  className="flex items-center gap-3 border-t border-hairline px-4 py-1.5 text-xs first:border-t-0"
                >
                  <span className="w-24 shrink-0 font-medium">{s.loop}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-2xs text-faint">
                    {s.scope}
                  </span>
                  <span className="tnum shrink-0 text-2xs text-muted">{s.lastRunAt ?? "—"}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
