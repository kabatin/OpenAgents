/**
 * 設定カタログの組み立てと、カタログを使った値の解決。
 *
 * 画面へ渡すのは「カタログ（何が設定できるか）」と「解決済みの値
 * （今どうなっているか＋既定値なのか明示設定なのか）」の2つ。
 */
import { agentGroups } from "./catalog.agent.ts";
import {
  DEV_BOT_GROUPS,
  GLOBAL_GROUPS,
  MEETING_BOT_GROUPS,
} from "./catalog.global.ts";
import { displayValue } from "./bigjson.ts";
import { getPath } from "./objpath.ts";
import { triFrom, type Setting, type SettingGroup } from "./types.ts";

export { agentGroups, DEV_BOT_GROUPS, GLOBAL_GROUPS, MEETING_BOT_GROUPS };
export * from "./types.ts";

/** グループ配列をフラットな Setting 一覧にする（children も含む）。 */
export function flatten(groups: SettingGroup[]): Setting[] {
  const out: Setting[] = [];
  const walk = (s: Setting) => {
    out.push(s);
    for (const c of s.children ?? []) walk(c);
  };
  for (const g of groups) for (const s of g.settings) walk(s);
  return out;
}

export type ResolvedValue = {
  path: string;
  /** 実際に効いている値（未設定なら既定値） */
  value: unknown;
  /** config.json に明示的に書かれているか */
  explicit: boolean;
  /** 依存する設定が満たされていないため、ONでも効かない状態か */
  blockedBy?: string[];
};

/**
 * 1つの Setting について、スコープのルートオブジェクトから現在値を解決する。
 * `tri` は {enabled, shadow} を 'off'|'shadow'|'live' に畳む。
 */
export function resolveValue(setting: Setting, scopeRoot: unknown): ResolvedValue {
  if (setting.kind === "tri") {
    const raw = getPath(scopeRoot, setting.path);
    const def = (setting.default ?? {}) as { shadow?: boolean };
    return {
      path: setting.path,
      value: triFrom(raw, def.shadow !== false),
      explicit: raw !== undefined,
    };
  }
  const raw = getPath(scopeRoot, setting.path);
  // 「親オブジェクトの存在＝有効」型（image_gen 等）: .enabled リーフが無くても
  // 親がオブジェクトなら ON と解決する
  if (raw === undefined && setting.presenceIsOn === true) {
    const parentPath = setting.path.split(".").slice(0, -1).join(".");
    const parent = getPath(scopeRoot, parentPath);
    if (typeof parent === "object" && parent !== null && !Array.isArray(parent)) {
      return { path: setting.path, value: true, explicit: true };
    }
  }
  return {
    path: setting.path,
    // 大きすぎる整数は内部で目印つき文字列として持っている。表示では素の数字列に戻す。
    value: raw === undefined ? (setting.default ?? null) : displayValue(raw),
    explicit: raw !== undefined,
  };
}

/**
 * 依存関係（requires）が満たされているかを判定する。
 * `$global:` 接頭辞のパスは config.json のルートから解決する。
 */
export function evaluateBlockers(
  setting: Setting,
  scopeRoot: unknown,
  configRoot: unknown,
): string[] {
  const blockers: string[] = [];
  for (const req of setting.requires ?? []) {
    const isGlobal = req.path.startsWith("$global:");
    const path = isGlobal ? req.path.slice("$global:".length) : req.path;
    const raw = getPath(isGlobal ? configRoot : scopeRoot, path);
    const satisfied = Array.isArray(raw) ? raw.length > 0 : Boolean(raw);
    if (!satisfied) blockers.push(req.label);
  }
  return blockers;
}

export type ResolvedGroup = {
  id: string;
  label: string;
  desc?: string;
  settings: ResolvedSetting[];
};

export type ResolvedSetting = Setting & {
  current: ResolvedValue;
  blockedBy: string[];
  children?: ResolvedSetting[];
};

export function resolveGroups(
  groups: SettingGroup[],
  scopeRoot: unknown,
  configRoot: unknown,
): ResolvedGroup[] {
  const resolve = (s: Setting): ResolvedSetting => {
    const current = resolveValue(s, scopeRoot);
    return {
      ...s,
      // 秘密情報は「設定されているか」だけを渡す。実値はここから先へ出さない。
      current: s.secret ? { ...current, value: maskSecret(current.value) } : current,
      blockedBy: evaluateBlockers(s, scopeRoot, configRoot),
      children: s.children?.map(resolve),
    };
  };
  return groups.map((g) => ({
    id: g.id,
    label: g.label,
    desc: g.desc,
    settings: g.settings.map(resolve),
  }));
}

/** 秘密情報のマスク表示（先頭4文字…末尾2文字）。値そのものは絶対に返さない。 */
export function maskSecret(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) return "（未設定）";
  if (value.length <= 8) return "設定済み";
  return `${value.slice(0, 4)}…${value.slice(-2)}（設定済み）`;
}
