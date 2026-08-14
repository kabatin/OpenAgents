/**
 * DiscordのスノーフレークID（19桁）が JSON の往復で壊れないことの検証。
 *
 * 実際に起きた事故: agent_category_id 1234567890123456789 が
 * JSON.parse → JSON.stringify で 1234567890123456800 に化けた。
 * 設定エディタが黙ってIDを書き換えるのは許されないので、ここで固定する。
 */
import fs from "node:fs";
import { describe, expect, it } from "vitest";

import { displayValue, isRawBigInt, parseJson, stringifyJson } from "../server/config/bigjson.ts";
import { applyPatches, getPath } from "../server/config/objpath.ts";
import { CONFIG_EXAMPLE_PATH } from "../server/paths.ts";

const SNOWFLAKE = "1234567890123456789";

describe("素の JSON では壊れることの確認（この事故の前提）", () => {
  it("JSON.parse は19桁の整数を丸めてしまう", () => {
    const roundTripped = JSON.stringify(JSON.parse(`{"id": ${SNOWFLAKE}}`));
    expect(roundTripped).not.toContain(SNOWFLAKE);
  });
});

describe("parseJson / stringifyJson", () => {
  it("19桁の整数を1桁も違えずに往復する", () => {
    const text = `{\n  "id": ${SNOWFLAKE}\n}`;
    expect(stringifyJson(parseJson(text))).toContain(SNOWFLAKE);
  });

  it("引用符なしの数値のまま出力する（型を文字列に変えない）", () => {
    const out = stringifyJson(parseJson(`{"id": ${SNOWFLAKE}}`));
    expect(out).toContain(`"id": ${SNOWFLAKE}`);
    expect(out).not.toContain(`"${SNOWFLAKE}"`);
  });

  it("安全な範囲の数値には目印を付けない", () => {
    const parsed = parseJson(`{"n": 42, "big": ${SNOWFLAKE}}`) as Record<string, unknown>;
    expect(parsed["n"]).toBe(42);
    expect(isRawBigInt(parsed["n"])).toBe(false);
    expect(isRawBigInt(parsed["big"])).toBe(true);
  });

  it("表示のときは目印を外して素の数字列にする", () => {
    const parsed = parseJson(`{"big": ${SNOWFLAKE}}`) as Record<string, unknown>;
    expect(displayValue(parsed["big"])).toBe(SNOWFLAKE);
  });

  it("文字列として書かれたIDはそのまま文字列", () => {
    const parsed = parseJson(`{"id": "${SNOWFLAKE}"}`) as Record<string, unknown>;
    expect(parsed["id"]).toBe(SNOWFLAKE);
    expect(stringifyJson(parsed)).toContain(`"id": "${SNOWFLAKE}"`);
  });

  it("小数や指数表記は触らない", () => {
    const out = stringifyJson(parseJson(`{"a": 1.5, "b": 1e3}`));
    expect(JSON.parse(out)).toEqual({ a: 1.5, b: 1000 });
  });
});

describe("同梱の config.example.json", () => {
  const raw = fs.readFileSync(CONFIG_EXAMPLE_PATH, "utf8");

  it("読んで書き戻すと元のファイルと完全一致する（末尾改行を除き）", () => {
    const parsed = parseJson(raw);
    expect(stringifyJson(parsed)).toBe(raw.trimEnd());
  });

  it("無関係な設定を1つ変えても、大きなIDは1桁も変わらない", () => {
    const parsed = parseJson(raw) as Record<string, unknown>;
    // 例ファイルの guild_id は19桁。素の JSON.parse ならここで下3桁が化ける
    const before = getPath(parsed, "guild_id");
    const next = applyPatches(parsed, [
      { path: "agents.0.proactive.interval_min", value: 45 },
    ]);
    expect(getPath(next, "guild_id")).toBe(before);
    const out = stringifyJson(next);
    expect(out).toContain(`"guild_id": 1234567890123456789`);
    expect(out).toContain(`"interval_min": 45`);
  });
});
