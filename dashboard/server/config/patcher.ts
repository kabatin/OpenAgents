/**
 * 画面からの変更要求を config.json のパッチへ翻訳する。
 *
 * ここが唯一の「書き込みの入口」。カタログに載っていない設定・readonly の設定・
 * 型や範囲の合わない値は、ファイルに触れる前にすべてここで弾く。
 */
import {
  agentGroups,
  DEV_BOT_GROUPS,
  flatten,
  GLOBAL_GROUPS,
  MEETING_BOT_GROUPS,
  triTo,
  type Setting,
} from "./catalog.ts";
import type { Patch } from "./store.ts";
import type { Json } from "./objpath.ts";

export type Scope = { kind: "global" } | { kind: "agent"; id: string } | { kind: "meeting" };

export class PatchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PatchError";
  }
}

export function parseScope(raw: string): Scope {
  if (raw === "global") return { kind: "global" };
  if (raw === "meeting") return { kind: "meeting" };
  const m = /^agent:(.+)$/.exec(raw);
  if (m?.[1]) return { kind: "agent", id: m[1] };
  throw new PatchError(`不明なスコープです: ${raw}`);
}

function catalogFor(scope: Scope): Setting[] {
  if (scope.kind === "agent") return flatten(agentGroups());
  if (scope.kind === "meeting") return flatten(MEETING_BOT_GROUPS);
  return [...flatten(GLOBAL_GROUPS), ...flatten(DEV_BOT_GROUPS)];
}

function findSetting(scope: Scope, path: string): Setting {
  const found = catalogFor(scope).find((s) => s.path === path);
  if (!found) throw new PatchError(`設定カタログに無いパスです: ${path}`);
  if (found.readonly) throw new PatchError(`「${found.label}」は画面から変更できません`);
  return found;
}

function asInt(setting: Setting, value: unknown): number {
  const n = typeof value === "string" ? Number(value) : value;
  if (typeof n !== "number" || !Number.isFinite(n) || !Number.isInteger(n)) {
    throw new PatchError(`「${setting.label}」には整数を指定してください`);
  }
  if (setting.min !== undefined && n < setting.min) {
    throw new PatchError(`「${setting.label}」は ${setting.min} 以上にしてください`);
  }
  if (setting.max !== undefined && n > setting.max) {
    throw new PatchError(`「${setting.label}」は ${setting.max} 以下にしてください`);
  }
  return n;
}

/** カタログの定義に沿って値を検証し、保存する形に正規化する。 */
function coerce(setting: Setting, value: unknown): Json {
  switch (setting.kind) {
    case "bool":
      if (typeof value !== "boolean") {
        throw new PatchError(`「${setting.label}」にはON/OFFを指定してください`);
      }
      return value;
    case "int":
      return asInt(setting, value);
    case "hour":
      return asInt({ ...setting, min: 0, max: 23 }, value);
    case "weekday":
      return asInt({ ...setting, min: 0, max: 6 }, value);
    case "monthday":
      return asInt({ ...setting, min: 1, max: 31 }, value);
    case "string":
    case "text": {
      if (value === null || value === "") return null;
      if (typeof value !== "string") {
        throw new PatchError(`「${setting.label}」には文字列を指定してください`);
      }
      return value;
    }
    case "enum": {
      const allowed = (setting.options ?? []).map((o) => o.value);
      if (!allowed.includes(value as string | number | null)) {
        throw new PatchError(`「${setting.label}」に指定できない値です`);
      }
      return value as Json;
    }
    case "stringList": {
      if (!Array.isArray(value) || value.some((v) => typeof v !== "string")) {
        throw new PatchError(`「${setting.label}」には文字列のリストを指定してください`);
      }
      return value as string[];
    }
    case "intList": {
      if (!Array.isArray(value) || value.some((v) => !Number.isInteger(v))) {
        throw new PatchError(`「${setting.label}」には整数のリストを指定してください`);
      }
      return value as number[];
    }
    case "info":
      throw new PatchError(`「${setting.label}」は表示専用です`);
    case "tri":
      throw new PatchError("内部エラー: tri は expandTri で処理してください");
  }
}

export type ChangeRequest = { path: string; value: unknown };

/**
 * 変更要求 → config.json のパッチ列。
 * 3値トグルは {enabled, shadow} の2パッチに展開する。
 */
export function toPatches(
  scope: Scope,
  changes: ChangeRequest[],
  agentIndexOf: (id: string) => number,
): Patch[] {
  const prefix =
    scope.kind === "agent"
      ? (() => {
          const idx = agentIndexOf(scope.id);
          if (idx < 0) throw new PatchError(`そんなエージェントは居ません: ${scope.id}`);
          return `agents.${idx}.`;
        })()
      : "";

  const patches: Patch[] = [];
  for (const change of changes) {
    const setting = findSetting(scope, change.path);
    if (setting.kind === "tri") {
      if (change.value !== "off" && change.value !== "shadow" && change.value !== "live") {
        throw new PatchError(`「${setting.label}」には OFF / シャドー / 本番 のいずれかを指定してください`);
      }
      const { enabled, shadow } = triTo(change.value);
      patches.push({ path: `${prefix}${setting.path}.enabled`, value: enabled });
      patches.push({ path: `${prefix}${setting.path}.shadow`, value: shadow });
      continue;
    }
    patches.push({ path: `${prefix}${setting.path}`, value: coerce(setting, change.value) });
  }
  return patches;
}
