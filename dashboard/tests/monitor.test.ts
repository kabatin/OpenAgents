/**
 * devbot/test_monitor.py の境界値ケースを移植したもの。
 * 画面とBOTで健全性の判定が食い違うと「どっちが本当か」が分からなくなるので、
 * Python側と同じ結論になることをここで固定する。
 */
import { describe, expect, it } from "vitest";

import {
  classify,
  confirm,
  DISCONNECTED,
  DOWN,
  heartbeatVerdict,
  IDLE,
  OK,
  STALLED,
  type ConfirmState,
  type Signals,
} from "../server/ops/monitor.ts";

const sig = (o: Partial<Signals> = {}): Signals => ({
  processAlive: true,
  discordOnline: true,
  logAgeSec: 0,
  lastExitStatus: null,
  ...o,
});

describe("classify", () => {
  it("すべて正常なら OK", () => {
    expect(classify(sig()).status).toBe(OK);
  });

  it("PIDが無ければ DOWN（最優先）", () => {
    const h = classify(sig({ processAlive: false, discordOnline: false, lastExitStatus: 1 }));
    expect(h.status).toBe(DOWN);
    expect(h.detail).toContain("exit=1");
  });

  it("生存しているがゲートウェイが切れていれば DISCONNECTED", () => {
    expect(classify(sig({ discordOnline: false })).status).toBe(DISCONNECTED);
  });

  it("オンライン状態が不明(null)の間は接続切れを主張しない", () => {
    expect(classify(sig({ discordOnline: null })).status).toBe(OK);
  });

  it("ログが閾値を超えて無音なら STALLED", () => {
    expect(classify(sig({ logAgeSec: 1801 }), { stallAfterSec: 1800 }).status).toBe(STALLED);
  });

  it("閾値ちょうどは STALLED にしない（境界）", () => {
    expect(classify(sig({ logAgeSec: 1800 }), { stallAfterSec: 1800 }).status).toBe(OK);
  });

  it("ログ鮮度が不明(null)なら無音判定をしない", () => {
    expect(classify(sig({ logAgeSec: null }), { stallAfterSec: 1 }).status).toBe(OK);
  });

  it("接続切れは無音より優先される", () => {
    expect(classify(sig({ discordOnline: false, logAgeSec: 99999 })).status).toBe(DISCONNECTED);
  });

  it("定期起動ジョブはPIDが無くても IDLE（異常ではない）", () => {
    expect(classify(sig({ processAlive: false }), { resident: false }).status).toBe(IDLE);
  });
});

describe("confirm（デバウンス）", () => {
  it("1回目の観測では確定しない", () => {
    const s: ConfirmState = { candidate: null, streak: 0 };
    expect(confirm(s, DISCONNECTED).confirmed).toBeNull();
  });

  it("2回続けば確定する", () => {
    let s: ConfirmState = { candidate: null, streak: 0 };
    let r = confirm(s, DISCONNECTED);
    s = r.state;
    r = confirm(s, DISCONNECTED);
    expect(r.confirmed).toBe(DISCONNECTED);
  });

  it("観測が揺れたら連続回数がリセットされる", () => {
    let s: ConfirmState = { candidate: null, streak: 0 };
    s = confirm(s, DISCONNECTED).state;
    s = confirm(s, OK).state; // 揺れ
    const r = confirm(s, DISCONNECTED);
    expect(r.confirmed).toBeNull();
  });

  it("DOWN だけは1回で即確定する（launchctlは確実な信号）", () => {
    const s: ConfirmState = { candidate: null, streak: 0 };
    expect(confirm(s, DOWN).confirmed).toBe(DOWN);
  });
});

describe("heartbeatVerdict", () => {
  it("閾値内なら正常", () => {
    expect(heartbeatVerdict(120).ok).toBe(true);
  });
  it("閾値ちょうどは正常（境界）", () => {
    expect(heartbeatVerdict(300).ok).toBe(true);
  });
  it("閾値を超えたら異常", () => {
    expect(heartbeatVerdict(301).ok).toBe(false);
  });
  it("取得できなければ判定しない", () => {
    expect(heartbeatVerdict(null).ok).toBeNull();
  });
});
