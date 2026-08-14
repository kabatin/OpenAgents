/**
 * 各サービスの現在状態を1枚にまとめる。
 *
 * 生存判定の材料は、スーパーバイザ（run.py）が返す
 *   プロセスの状態 → 生存証明の鮮度 → ログの鮮度
 * の順。**プロセスが生きていることと、仕事ができていることは別物**なので、
 * プロセスの存在だけでは「稼働中」と言わない。
 */
import { LOGS_DIR, SERVICE_BY_ID, SERVICES, type ServiceId } from "../paths.ts";
import path from "node:path";
import {
  classify,
  confirm,
  heartbeatVerdict,
  STATUS_LABEL,
  type ConfirmState,
  type Health,
  type HealthStatus,
} from "./monitor.ts";
import {
  fileAgeSec,
  fileSize,
  restartViaSupervisor,
  supervisorStatus,
  type SupervisorService,
} from "./probes.ts";

export type ServiceStatus = {
  id: ServiceId;
  label: string;
  /** 設定でオフにされている（異常ではない） */
  enabled: boolean;
  note?: string;
  pid: number | null;
  /** スーパーバイザが数えている再起動回数（クラッシュループの兆候） */
  restarts: number;
  uptimeSec: number | null;
  lastExit: string | null;
  logAgeSec: number | null;
  logSizeBytes: number | null;
  heartbeatAgeSec: number | null;
  heartbeatDetail: string | null;
  status: HealthStatus;
  statusLabel: string;
  detail: string;
  /** デバウンス前の生の観測（表示はしないがデバッグ用に持たせる） */
  observed: HealthStatus;
};

/** サービスごとのデバウンス状態（プロセス内で保持） */
const confirmStates = new Map<ServiceId, ConfirmState>();
const lastConfirmed = new Map<ServiceId, HealthStatus>();

function logPathFor(id: ServiceId): string {
  return path.join(LOGS_DIR, `${id}.log`);
}

async function buildStatus(
  id: ServiceId,
  label: string,
  note: string | undefined,
  svc: SupervisorService | null,
  supervisorError: string | null,
  stallAfterSec: number,
): Promise<ServiceStatus> {
  const now = Date.now();
  const logPath = svc?.logPath ?? logPathFor(id);
  const [logAgeSec, logSizeBytes] = await Promise.all([
    fileAgeSec(logPath, now),
    fileSize(logPath),
  ]);

  // スーパーバイザに繋がらない = BOTの状態は分からない。
  // 「停止」と断定すると、実は動いているのに人を慌てさせる
  if (svc === null) {
    const status: HealthStatus = "unknown";
    return {
      id, label, note, enabled: false,
      pid: null, restarts: 0, uptimeSec: null, lastExit: null,
      logAgeSec, logSizeBytes,
      heartbeatAgeSec: null, heartbeatDetail: null,
      status, statusLabel: STATUS_LABEL[status],
      detail: supervisorError ?? "状態を取得できませんでした",
      observed: status,
    };
  }

  const heartbeatAgeSec = svc.heartbeatAgeSec;
  // staleAfterSec = 0 は「鮮度では判断しない」対象（議事録BOT）
  const watchesHeartbeat = svc.staleAfterSec > 0;
  const heartbeatDetail = watchesHeartbeat
    ? heartbeatVerdict(heartbeatAgeSec).detail
    : null;

  // 生存証明が失効している = プロセスは居るがループが止まっている
  let alive: boolean | null = null;
  if (watchesHeartbeat && heartbeatAgeSec !== null) {
    alive = heartbeatVerdict(heartbeatAgeSec).ok;
  }

  const health: Health = classify(
    {
      processAlive: svc.pid !== null,
      discordOnline: alive,
      logAgeSec: watchesHeartbeat ? logAgeSec : null,
      lastExitStatus: null,
    },
    { stallAfterSec, resident: true },
  );

  let observed = health.status;
  let detail = health.detail;
  if (!svc.enabled) {
    observed = "idle";
    detail = "設定でオフになっています";
  } else if (svc.state === "restarting") {
    observed = "down";
    detail = `再起動を待っています（${svc.lastExit ?? "終了理由不明"}）`;
  } else if (svc.state === "error") {
    observed = "down";
    detail = svc.lastExit ?? "起動できませんでした";
  }

  const prevState = confirmStates.get(id) ?? { candidate: null, streak: 0 };
  const { state, confirmed } = confirm(prevState, observed);
  confirmStates.set(id, state);
  if (confirmed !== null) lastConfirmed.set(id, confirmed);
  const shown = lastConfirmed.get(id) ?? observed;

  return {
    id, label, note: svc.note ?? note,
    enabled: svc.enabled,
    pid: svc.pid,
    restarts: svc.restarts,
    uptimeSec: svc.uptimeSec,
    lastExit: svc.lastExit,
    logAgeSec, logSizeBytes,
    heartbeatAgeSec, heartbeatDetail,
    status: shown,
    statusLabel: STATUS_LABEL[shown],
    detail,
    observed,
  };
}

export async function allServiceStatus(stallAfterSec: number): Promise<ServiceStatus[]> {
  const snapshot = await supervisorStatus();
  const byId = new Map(snapshot.services.map((s) => [s.id, s]));
  return Promise.all(
    SERVICES.map((def) =>
      buildStatus(
        def.id,
        def.label,
        def.note,
        byId.get(def.id) ?? null,
        snapshot.error,
        stallAfterSec,
      ),
    ),
  );
}

export async function serviceStatus(
  id: ServiceId,
  stallAfterSec: number,
): Promise<ServiceStatus | null> {
  const def = SERVICE_BY_ID.get(id);
  if (!def) return null;
  const all = await allServiceStatus(stallAfterSec);
  return all.find((s) => s.id === id) ?? null;
}

export type RestartResult = {
  id: ServiceId;
  label: string;
  ok: boolean;
  detail: string;
};

/**
 * サービスを再起動する。
 * 実際の落として起こす作業はスーパーバイザが行うので、ここは依頼するだけ。
 */
export async function restartService(id: ServiceId): Promise<RestartResult> {
  const def = SERVICE_BY_ID.get(id);
  if (!def) throw new Error(`不明なサービスです: ${id}`);
  try {
    await restartViaSupervisor(id);
  } catch (e) {
    return {
      id,
      label: def.label,
      ok: false,
      detail: e instanceof Error ? e.message : String(e),
    };
  }
  // 再起動したので判定のデバウンスをやり直す
  confirmStates.delete(id);
  lastConfirmed.delete(id);
  return { id, label: def.label, ok: true, detail: "再起動を依頼しました" };
}
