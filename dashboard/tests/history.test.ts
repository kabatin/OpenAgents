/**
 * セットアップでの過去ログの取り込み範囲（archive.since）。
 * Python 側（core/archive_window.py）と同じ形式を書き、同じ形式だけを通す。
 */
import { describe, expect, it } from "vitest";

import { checkInvariants } from "../server/config/schema.ts";
import { isValidSince, sinceFromChoice } from "../server/setup/history.ts";

// 2026-10-05 13:05 JST
const NOW = new Date("2026-10-05T04:05:00Z");

describe("sinceFromChoice", () => {
  it("取り込まない = セットアップした時刻（日本時間）から", () => {
    expect(sinceFromChoice("none", undefined, NOW)).toBe("2026-10-05T13:05:00+09:00");
  });

  it("直近N日 = 日本時間で N 日前の0時から", () => {
    expect(sinceFromChoice("days", 90, NOW)).toBe("2026-07-07");
    expect(sinceFromChoice("days", 1, NOW)).toBe("2026-10-04");
  });

  it("すべて = 起点なし", () => {
    expect(sinceFromChoice("all", undefined, NOW)).toBeNull();
  });

  it("日数と選択肢の不正は弾く（巨大な取り込みに黙って倒れない）", () => {
    expect(() => sinceFromChoice("days", 0, NOW)).toThrow();
    expect(() => sinceFromChoice("days", 99999, NOW)).toThrow();
    expect(() => sinceFromChoice("days", Number.NaN, NOW)).toThrow();
    expect(() => sinceFromChoice("forever" as never, undefined, NOW)).toThrow();
  });
});

describe("isValidSince", () => {
  it("Python 側が読める形式だけを通す", () => {
    for (const ok of [null, "", "2026-07-01", "2026-07-01T09:30:00+09:00", "2026-07-01T09:30:00Z", "2026-07-01T09:30:00"]) {
      expect(isValidSince(ok), String(ok)).toBe(true);
    }
    for (const bad of ["昨日", "2026/07/01", "2026-13-01", 90, "2026-07-01 09:30"]) {
      expect(isValidSince(bad), String(bad)).toBe(false);
    }
  });

  it("不正な値は保存させない（BOTが起動しなくなるため）", () => {
    const issues = checkInvariants({ agents: [], archive: { since: "昨日" } });
    expect(issues.some((i) => i.path === "archive.since")).toBe(true);
    const ok = checkInvariants({ agents: [], archive: { since: "2026-07-01" } });
    expect(ok.filter((i) => i.path === "archive.since")).toEqual([]);
  });
});
