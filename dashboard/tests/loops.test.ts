import { describe, expect, it } from "vitest";

import { ageDays, isStale, LOOP_INFO, loopInfo } from "../web/lib/loops.ts";

// 2026-09-28 12:00 JST
const NOW = Date.UTC(2026, 8, 28, 3, 0);

describe("サブループの表示", () => {
  it("機能コードを日本語名にする（未登録はコードのまま）", () => {
    expect(loopInfo("svdistill").label).toBe("自己採点のまとめ");
    expect(loopInfo("zzz").label).toBe("zzz");
  });

  it("経過日数は JST の文字列から計算する", () => {
    expect(ageDays("2026-09-27T12:00", NOW)).toBeCloseTo(1, 5);
    expect(ageDays(null, NOW)).toBeNull();
    expect(ageDays("読めない", NOW)).toBeNull();
  });

  it("止まっているかは機能ごとの周期で決める（月次は3週間前でも正常）", () => {
    expect(isStale("pulse", "2026-09-01T11:04", NOW)).toBe(false);
    expect(isStale("minutes", "2026-09-25T10:00", NOW)).toBe(true);
    expect(isStale("minutes", "2026-09-28T10:00", NOW)).toBe(false);
    expect(isStale("minutes", null, NOW)).toBe(true);
  });

  it("廃止・停止の印が付いた機能は、最終実行が新しくても止まっている側", () => {
    LOOP_INFO["old_feature"] = { label: "x", desc: "x", everyDays: 31, retired: true };
    try {
      expect(isStale("old_feature", "2026-09-27T15:21", NOW)).toBe(true);
    } finally {
      delete LOOP_INFO["old_feature"];
    }
  });
});
