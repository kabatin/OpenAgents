/**
 * スーパーバイザ（run.py）への問い合わせ。
 *
 * 以前は `launchctl` と `lsof` を直接叩いていたが、それでは macOS でしか
 * 動かなかった。BOTの起動・監視・再起動はスーパーバイザが引き受けるので、
 * ここは **その1つに聞くだけ**になり、OSによる分岐が消えた。
 *
 * ここは「事実を集めるだけ」で、取れなければ null を返す
 * （取得できないことと異常であることを混同しない）。
 */
import fsp from "node:fs/promises";

/** スーパーバイザは 127.0.0.1 でしか待ち受けない（core/control.py と対応）。 */
const SUPERVISOR_HOST = "127.0.0.1";
const DEFAULT_SUPERVISOR_PORT = 8788;
const REQUEST_TIMEOUT_MS = 5_000;

export type SupervisorService = {
  id: string;
  label: string;
  enabled: boolean;
  /** running / starting / restarting / stopped / disabled / error */
  state: string;
  pid: number | null;
  restarts: number;
  failures: number;
  uptimeSec: number | null;
  lastExit: string | null;
  heartbeatAgeSec: number | null;
  staleAfterSec: number;
  note?: string;
  logPath?: string;
};

export type SupervisorSnapshot = {
  /** false = run.py が動いていない（＝BOTは1つも動いていない） */
  reachable: boolean;
  services: SupervisorService[];
  /** 到達できなかった理由（人間に見せる） */
  error: string | null;
};

function supervisorPort(): number {
  const raw = process.env["OPENAGENTS_SUPERVISOR_PORT"];
  const parsed = raw === undefined ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : DEFAULT_SUPERVISOR_PORT;
}

function baseUrl(): string {
  return `http://${SUPERVISOR_HOST}:${supervisorPort()}`;
}

async function request(path: string, method: "GET" | "POST"): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${baseUrl()}${path}`, {
      method,
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      const message = typeof body["message"] === "string" ? body["message"] : `HTTP ${res.status}`;
      throw new Error(message);
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

/** 全サービスの現況。run.py が動いていなければ reachable:false。 */
export async function supervisorStatus(): Promise<SupervisorSnapshot> {
  try {
    const body = (await request("/status", "GET")) as { services?: SupervisorService[] };
    return { reachable: true, services: body.services ?? [], error: null };
  } catch (e) {
    const cause = e instanceof Error ? e.message : String(e);
    return {
      reachable: false,
      services: [],
      error:
        `常駐プロセス（run.py）に接続できません: ${cause}。` +
        "ターミナルで `python run.py` を起動してください",
    };
  }
}

/** サービスを再起動する。スーパーバイザ側が落として起こし直す。 */
export async function restartViaSupervisor(id: string): Promise<void> {
  await request(`/restart/${encodeURIComponent(id)}`, "POST");
}

export async function stopViaSupervisor(id: string): Promise<void> {
  await request(`/stop/${encodeURIComponent(id)}`, "POST");
}

export async function startViaSupervisor(id: string): Promise<void> {
  await request(`/start/${encodeURIComponent(id)}`, "POST");
}

/** ファイルの最終更新からの経過秒（無ければ null）。 */
export async function fileAgeSec(path: string, nowMs = Date.now()): Promise<number | null> {
  try {
    const stat = await fsp.stat(path);
    return Math.max(0, (nowMs - stat.mtimeMs) / 1000);
  } catch {
    return null;
  }
}

/** ファイルサイズ（無ければ null）。ログ肥大の可視化に使う。 */
export async function fileSize(path: string): Promise<number | null> {
  try {
    return (await fsp.stat(path)).size;
  } catch {
    return null;
  }
}
