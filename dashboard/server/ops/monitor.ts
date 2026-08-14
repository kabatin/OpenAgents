/**
 * 健全性の判定ロジック（純粋関数）。
 *
 * devbot/monitor.py の classify / confirm を移植したもの。
 * **判定基準を2つ持つとダッシュボードとBOTで違うことを言い出す**ので、
 * 優先順位・閾値・状態名はPython側と厳密に揃えてある。
 * 対応するテスト: devbot/test_monitor.py → tests/monitor.test.ts
 */

export const OK = "ok";
export const DOWN = "down"; // launchctl にPIDが無い
export const DISCONNECTED = "disconnected"; // 生存しているがゲートウェイが切れている
export const STALLED = "stalled"; // 生存・接続中だがログが極端に無音
export const IDLE = "idle"; // 定期起動ジョブ（PIDが無いのが正常）
export const UNKNOWN = "unknown";

export type HealthStatus =
  | typeof OK
  | typeof DOWN
  | typeof DISCONNECTED
  | typeof STALLED
  | typeof IDLE
  | typeof UNKNOWN;

export const STATUS_LABEL: Record<HealthStatus, string> = {
  [OK]: "稼働中",
  [DOWN]: "停止",
  [DISCONNECTED]: "接続切れ",
  [STALLED]: "無音（要確認）",
  [IDLE]: "待機中",
  [UNKNOWN]: "不明",
};

export const DEFAULT_STALL_AFTER_SEC = 1800;

export type Signals = {
  processAlive: boolean;
  /** null = 未取得。不明な間は「接続切れ」を主張しない */
  discordOnline: boolean | null;
  /** null = ログ未取得、または鮮度を信用してはいけない対象 */
  logAgeSec: number | null;
  lastExitStatus: number | null;
};

export type Health = { status: HealthStatus; detail: string };

/**
 * 観測結果を状態へ写す。優先順位: 停止 > 接続切れ > 無音 > 正常。
 * `resident: false` の定期起動ジョブは、PIDが無いのが正常なので IDLE を返す。
 */
export function classify(
  sig: Signals,
  opts: { stallAfterSec?: number; resident?: boolean } = {},
): Health {
  const stallAfterSec = opts.stallAfterSec ?? DEFAULT_STALL_AFTER_SEC;
  const resident = opts.resident ?? true;

  if (!sig.processAlive) {
    if (!resident) return { status: IDLE, detail: "次の起動を待っています" };
    const tail = sig.lastExitStatus === null ? "" : `（最終exit=${sig.lastExitStatus}）`;
    return { status: DOWN, detail: `プロセスが停止しています${tail}` };
  }
  if (sig.discordOnline === false) {
    return {
      status: DISCONNECTED,
      detail: "プロセスは生きていますが、Discordへの接続が切れています",
    };
  }
  if (sig.logAgeSec !== null && sig.logAgeSec > stallAfterSec) {
    const mins = Math.floor(sig.logAgeSec / 60);
    return { status: STALLED, detail: `接続中ですがログが約${mins}分無音です（要確認）` };
  }
  return { status: OK, detail: "正常稼働中" };
}

export const CONFIRM_AFTER = 2;

export type ConfirmState = { candidate: HealthStatus | null; streak: number };

/**
 * 観測をデバウンスする。ゲートウェイの瞬断で表示が往復するのを防ぐ。
 * DOWN だけは launchctl の確実な信号なので即確定。
 */
export function confirm(
  state: ConfirmState,
  observed: HealthStatus,
  need = CONFIRM_AFTER,
): { state: ConfirmState; confirmed: HealthStatus | null } {
  if (observed === DOWN) {
    return { state: { candidate: observed, streak: need }, confirmed: observed };
  }
  const streak = observed === state.candidate ? state.streak + 1 : 1;
  const next: ConfirmState = { candidate: observed, streak };
  return { state: next, confirmed: streak >= need ? observed : null };
}

/** heartbeat の失効閾値。bot-watchdog/watchdog.sh の STALE_SEC と揃えてある。 */
export const HEARTBEAT_STALE_SEC = 300;

export function heartbeatVerdict(ageSec: number | null): {
  ok: boolean | null;
  detail: string;
} {
  if (ageSec === null) return { ok: null, detail: "生存証明がありません" };
  if (ageSec <= HEARTBEAT_STALE_SEC) {
    return { ok: true, detail: `生存証明は${Math.floor(ageSec)}秒前` };
  }
  return {
    ok: false,
    detail: `生存証明が${Math.floor(ageSec)}秒無音（閾値${HEARTBEAT_STALE_SEC}秒）— ウォッチドッグが再起動するはずです`,
  };
}
