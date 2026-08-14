/**
 * 「保存したが、まだBOTに反映されていない変更」の管理。
 *
 * config.json はプロセス起動時に1回しか読まれない（リロード機構が無い）。
 * したがって画面は「ファイルの中身」と「動いているプロセスが読んだ中身」を
 * 別物として扱い、その差を未適用として見せる必要がある。
 *
 * 仕組み: サービスを再起動するたびに、その時点の設定ファイルを
 * applied/<service>.json へ焼き付ける。現在のファイルとの差分＝未適用。
 * ターミナルから手で再起動された場合は「プロセス起動時刻 > ファイル更新時刻」で
 * 検知して焼き直す（画面が嘘をつかないように）。
 */
import fsp from "node:fs/promises";
import path from "node:path";

import { APPLIED_DIR, type ServiceId } from "../paths.ts";
import { parseJson, stringifyJson } from "./bigjson.ts";
import { diffJson, type Diff, type Json } from "./objpath.ts";

/** 設定を読むサービス（＝再起動が要るサービス）だけを扱う */
export type ConfigOwner = Extract<ServiceId, "archivebot" | "devbot" | "meetingbot">;

export const CONFIG_OWNERS: ConfigOwner[] = ["archivebot", "devbot", "meetingbot"];

/**
 * 再起動の対象を人に見せる名前。
 * 会話エージェントは何体でも増やせるので、固定の名前は持たない
 * （実際の名前は設定から来る。ownerLabel() を使う）。
 */
export const OWNER_LABEL: Record<ConfigOwner, string> = {
  archivebot: "会話エージェント",
  devbot: "開発BOT",
  meetingbot: "議事録BOT",
};

/** 設定に登録されている名前を並べたラベル（例「あかり・ひかり」）。 */
export function ownerLabel(owner: ConfigOwner, agentNames: string[]): string {
  if (owner !== "archivebot" || agentNames.length === 0) return OWNER_LABEL[owner];
  if (agentNames.length > 3) return `会話エージェント${agentNames.length}体`;
  return agentNames.join("・");
}

/**
 * config.json の中の1つのパスが、どのサービスの再起動を要求するか。
 * guild_id と admins は両プロセスが読むので両方に効く。
 */
export function ownersForPath(configPath: string): ConfigOwner[] {
  if (configPath.startsWith("dev_bot.") || configPath === "dev_bot") return ["devbot"];
  if (configPath === "guild_id" || configPath.startsWith("admins")) {
    return ["archivebot", "devbot"];
  }
  return ["archivebot"];
}

function snapshotFile(owner: ConfigOwner): string {
  return path.join(APPLIED_DIR, `${owner}.json`);
}

async function readSnapshot(owner: ConfigOwner): Promise<Record<string, Json> | null> {
  try {
    // 本体と同じ読み方をしないと、桁の丸めだけで偽の差分が出続ける
    return parseJson(await fsp.readFile(snapshotFile(owner), "utf8")) as Record<string, Json>;
  } catch {
    return null;
  }
}

export async function writeSnapshot(
  owner: ConfigOwner,
  config: Record<string, Json>,
): Promise<void> {
  await fsp.mkdir(APPLIED_DIR, { recursive: true });
  await fsp.writeFile(snapshotFile(owner), `${stringifyJson(config)}\n`, "utf8");
}

export type PendingChange = Diff & { owners: ConfigOwner[] };

export type PendingForOwner = {
  owner: ConfigOwner;
  label: string;
  changes: Diff[];
};

export type Pending = {
  changes: Diff[];
  /**
   * 「変更があるのは確かだが、中身が分からない」状態。
   * ダッシュボードを使い始める前にエディタで config.json を直した場合に起きる。
   * 差分ゼロと同じ扱いにすると画面が嘘をつくので、区別して伝える。
   */
  unknownStale: boolean;
};

/**
 * 未適用の差分を求める。
 *
 * @param owner        対象サービス
 * @param current      現在のファイル内容
 * @param configMtimeMs 現在のファイルの更新時刻
 * @param processStartMs 動いているプロセスの起動時刻（null=停止中）
 */
export async function pendingFor(
  owner: ConfigOwner,
  current: Record<string, Json>,
  configMtimeMs: number,
  processStartMs: number | null,
): Promise<Pending> {
  // プロセスが現在のファイルより後に起動しているなら、それが読んだのは今の内容。
  // 外部から手で再起動された場合もここで追いつく。
  if (processStartMs !== null && processStartMs > configMtimeMs) {
    await writeSnapshot(owner, current);
    return { changes: [], unknownStale: false };
  }

  const snap = await readSnapshot(owner);
  if (snap === null) {
    // 記録が無い。プロセスがファイルより古いなら、読んだ内容は今と違う。
    // 何が違うかは分からないので、それをそのまま伝える。
    const stale = processStartMs !== null && processStartMs < configMtimeMs;
    await writeSnapshot(owner, current);
    return { changes: [], unknownStale: stale };
  }

  const all = diffJson(snap, current);
  const changes =
    owner === "meetingbot" ? all : all.filter((d) => ownersForPath(d.path).includes(owner));
  return { changes, unknownStale: false };
}
