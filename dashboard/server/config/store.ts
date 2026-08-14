/**
 * config.json の読み書き。
 *
 * 書き込みの鉄則:
 *   1. 読んだJSONの該当パスだけを差し替える（未知キー・キー順を保存）
 *   2. 検証を通ってから書く（起動しなくなる設定は拒否）
 *   3. 書く前に必ずバックアップを取る
 *   4. tmp + rename でアトミックに置き換える（BOTが読んでいる最中の壊れ防止）
 */
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

import { BACKUPS_DIR, CONFIG_EXAMPLE_PATH, CONFIG_PATH } from "../paths.ts";
import { parseJson, stringifyJson } from "./bigjson.ts";
import { appendTo, applyPatches, getPath, removeFrom, type Json } from "./objpath.ts";
import {
  checkInvariants,
  meetingConfigSchema,
  type MeetingConfig,
  type ValidationIssue,
} from "./schema.ts";

export type Patch = { path: string; value: unknown };

export class ConfigError extends Error {
  constructor(
    message: string,
    readonly issues: ValidationIssue[] = [],
  ) {
    super(message);
    this.name = "ConfigError";
  }
}

async function readJson(file: string): Promise<Record<string, Json>> {
  let raw: string;
  try {
    raw = await fsp.readFile(file, "utf8");
  } catch (error) {
    throw new ConfigError(`設定ファイルを読めませんでした: ${file}（${String(error)}）`);
  }
  try {
    // 素の JSON.parse は使わない — Discord ID(19桁)が丸められる。bigjson.ts 参照。
    return parseJson(raw) as Record<string, Json>;
  } catch (error) {
    throw new ConfigError(`設定ファイルのJSONが壊れています: ${file}（${String(error)}）`);
  }
}

/** 保存前のスナップショット。壊したときはここから戻す。 */
async function backup(file: string): Promise<string> {
  await fsp.mkdir(BACKUPS_DIR, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dest = path.join(BACKUPS_DIR, `${path.basename(file)}.${stamp}.json`);
  await fsp.copyFile(file, dest);
  await pruneBackups(path.basename(file));
  return dest;
}

/** バックアップは直近50世代だけ残す（無限に増やさない）。 */
async function pruneBackups(prefix: string, keep = 50): Promise<void> {
  const entries = (await fsp.readdir(BACKUPS_DIR))
    .filter((f) => f.startsWith(`${prefix}.`))
    .sort();
  for (const stale of entries.slice(0, Math.max(0, entries.length - keep))) {
    await fsp.rm(path.join(BACKUPS_DIR, stale), { force: true });
  }
}

// 書き込みの直列化。Hono はリクエストを並行に処理するので、
// 「読む → 加工 → 書く」の区間が重なると**後勝ちで前の変更が消える**
// （2人が同時に別の設定を保存すると片方が無かったことになる）。
// 全ての read-modify-write をこの鎖に繋いで1本ずつ流す。
let writeChain: Promise<unknown> = Promise.resolve();

function serialized<T>(fn: () => Promise<T>): Promise<T> {
  const next = writeChain.then(fn, fn);
  // 失敗しても鎖は切らない（次の書き込みまで巻き添えにしない）
  writeChain = next.catch(() => undefined);
  return next;
}

let tmpCounter = 0;

/**
 * tmp に書いて rename。同一ディレクトリ内の rename は原子的なので、
 * BOTが同時に読んでも中途半端なJSONを掴まない。元ファイルの権限は維持する。
 */
async function writeAtomic(file: string, data: unknown): Promise<void> {
  // tmp名は毎回変える。共通名だと、万一書き込みが重なったときに
  // 互いのファイルを踏んで「rename すればアトミック」の前提が崩れる
  tmpCounter += 1;
  const tmp = `${file}.${process.pid}.${tmpCounter}.tmp`;
  const body = `${stringifyJson(data)}\n`;
  const mode = fs.existsSync(file) ? (await fsp.stat(file)).mode : 0o600;
  const handle = await fsp.open(tmp, "w", mode);
  try {
    await handle.writeFile(body, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  await fsp.rename(tmp, file);
}

/**
 * 設定を読む。**まだ無ければ空を返す**（初回起動は正常な状態）。
 *
 * ここで例外にすると、設定を作るための画面が開けないうえ、
 * 一定間隔で走る更新のたびにスタックトレースが流れて
 * 「壊れている」ように見えてしまう。Python 側（core/config.py）と同じ扱い。
 */
export async function readConfig(): Promise<Record<string, Json>> {
  if (!configExists()) return {};
  return readJson(CONFIG_PATH);
}

/** 議事録BOTの設定。config.json の meeting_bot セクションを見る。 */
export async function readMeetingConfig(): Promise<MeetingConfig> {
  const root = await readConfig();
  const raw = (root["meeting_bot"] ?? {}) as Record<string, Json>;
  const parsed = meetingConfigSchema.safeParse(raw);
  if (!parsed.success) {
    throw new ConfigError(
      "議事録BOTの設定が想定と違います",
      parsed.error.errors.map((e) => ({ path: e.path.join("."), message: e.message })),
    );
  }
  return raw as MeetingConfig;
}

export type WriteResult = {
  backupPath: string;
  applied: Patch[];
  config: Record<string, Json>;
};

/**
 * config.json へパッチを適用して保存する。
 * 検証に落ちたら1バイトも書かずに ConfigError を投げる。
 */
export function patchConfig(patches: Patch[]): Promise<WriteResult> {
  return serialized(() => patchConfigLocked(patches));
}

async function patchConfigLocked(patches: Patch[]): Promise<WriteResult> {
  if (patches.length === 0) throw new ConfigError("変更がありません");
  const current = await readConfig();
  const next = applyPatches(current, patches);

  const issues = checkInvariants(next);
  if (issues.length > 0) {
    throw new ConfigError("この変更ではBOTが起動しなくなります", issues);
  }

  const backupPath = await backup(CONFIG_PATH);
  await writeAtomic(CONFIG_PATH, next);
  return { backupPath, applied: patches, config: next };
}

/**
 * 議事録BOTの設定へのパッチ（話者名マッピングなど）。
 * 保存先は config.json の meeting_bot セクション（設定ファイルは1枚だけ）。
 */
export function patchMeetingConfig(patches: Patch[]): Promise<WriteResult> {
  return serialized(() => patchMeetingConfigLocked(patches));
}

async function patchMeetingConfigLocked(patches: Patch[]): Promise<WriteResult> {
  if (patches.length === 0) throw new ConfigError("変更がありません");

  // カタログ上のパスはセクション相対なので、保存時に接頭辞を足す
  const prefixed = patches.map((p) => ({ ...p, path: `meeting_bot.${p.path}` }));
  const current = await readConfig();
  const next = applyPatches(current, prefixed);

  const section = (next["meeting_bot"] ?? {}) as Record<string, Json>;
  const parsed = meetingConfigSchema.safeParse(section);
  if (!parsed.success) {
    throw new ConfigError(
      "この変更は議事録BOTの設定として不正です",
      parsed.error.errors.map((e) => ({ path: e.path.join("."), message: e.message })),
    );
  }

  const backupPath = await backup(CONFIG_PATH);
  await writeAtomic(CONFIG_PATH, next);
  return { backupPath, applied: prefixed, config: next };
}

/**
 * config.json の最終更新時刻（未適用判定の補助に使う）。
 * まだ無ければ 0（初回起動は正常な状態。ここで投げると更新のたびに
 * スタックトレースが流れて「壊れている」ように見える）。
 */
export async function configMtimeMs(): Promise<number> {
  try {
    return (await fsp.stat(CONFIG_PATH)).mtimeMs;
  } catch {
    return 0;
  }
}


// ---------------------------------------------------------------------------
// 初期設定モード
//
// config.json がまだ無い状態は「異常」ではなく「これから作る」状態。
// ここで例外にすると、設定を作るための画面すら開けなくなる。
// ---------------------------------------------------------------------------

/** config.json が既にあるか。 */
export function configExists(): boolean {
  return fs.existsSync(CONFIG_PATH);
}

/**
 * 同梱の例をひな形にして config.json を作る。
 * 既にあれば何もしない（利用者の設定を絶対に踏まない）。
 */
export function createInitialConfig(): Promise<{ created: boolean }> {
  return serialized(async () => {
    if (configExists()) return { created: false };
  const example = await readJson(CONFIG_EXAMPLE_PATH);
  // 例に入っているダミーのIDは消しておく。残すと「設定済みに見えて動かない」
  const blank = applyPatches(example, [
    { path: "guild_id", value: "" },
    { path: "admins", value: [] },
    { path: "agents", value: [] },
  ]);
    await fsp.mkdir(path.dirname(CONFIG_PATH), { recursive: true });
    await writeAtomic(CONFIG_PATH, blank);
    return { created: true };
  });
}

// ---------------------------------------------------------------------------
// エージェントの増減
// ---------------------------------------------------------------------------

export type NewAgent = {
  id: string;
  name: string;
  token: string;
  home_channel_id: string;
  persona_files: string[];
  role?: string;
  archiver?: boolean;
  require_mention?: boolean;
};

const ID_RE = /^[a-z][a-z0-9_-]{0,31}$/;

/** エージェントを1体足す。1体目は自動的に会話の記録担当になる。 */
export function addAgent(agent: NewAgent): Promise<WriteResult> {
  return serialized(() => addAgentLocked(agent));
}

async function addAgentLocked(agent: NewAgent): Promise<WriteResult> {
  // --- 指摘3: 必須項目はサーバー側でも検査する（UIのバグに設定を壊させない）。
  // 空のまま保存できると、保存は成功するのに次の起動が拒否される
  if (agent.name.trim() === "") throw new ConfigError("表示名が空です");
  if (agent.token.trim() === "") throw new ConfigError("Botトークンが空です");
  if (agent.home_channel_id.trim() === "") {
    throw new ConfigError("ホームチャンネルが選ばれていません");
  }
  if (!ID_RE.test(agent.id)) {
    throw new ConfigError(
      "エージェントIDは英小文字で始まる32文字以内の英数字（_ - 可）にしてください",
    );
  }
  const current = await readConfig();
  const agents = (getPath(current, "agents") ?? []) as { id?: string }[];
  if (agents.some((a) => a.id === agent.id)) {
    throw new ConfigError(`エージェントID「${agent.id}」は既に使われています`);
  }
  // 記録担当はちょうど1体。まだ居なければこの子が担当になる
  const hasArchiver = agents.some((a) => (a as { archiver?: boolean }).archiver === true);
  const entry: Record<string, Json> = {
    id: agent.id,
    name: agent.name,
    token: agent.token,
    home_channel_id: agent.home_channel_id,
    archiver: hasArchiver ? false : true,
    persona_files: agent.persona_files,
    role: agent.role ?? "",
    require_mention: agent.require_mention ?? Boolean(hasArchiver),
    skills: { reminder: true, youtube_summary: true, pdf_summary: true },
    proactive: { enabled: false },
  };
  const next = appendTo(current, "agents", entry);

  const issues = checkInvariants(next);
  if (issues.length > 0) {
    throw new ConfigError("この内容ではBOTが起動しなくなります", issues);
  }
  const backupPath = await backup(CONFIG_PATH);
  await writeAtomic(CONFIG_PATH, next);
  return { backupPath, applied: [{ path: `agents.${agent.id}`, value: entry }], config: next };
}

/**
 * エージェントを1体消す。
 * 会話の記録担当は消せない（過去ログの取り込みが止まるため）。
 */
export function removeAgent(id: string): Promise<WriteResult> {
  return serialized(() => removeAgentLocked(id));
}

async function removeAgentLocked(id: string): Promise<WriteResult> {
  const current = await readConfig();
  const agents = (getPath(current, "agents") ?? []) as { id?: string; archiver?: boolean }[];
  const target = agents.find((a) => a.id === id);
  if (!target) throw new ConfigError(`エージェント「${id}」が見つかりません`);
  if (target.archiver === true && agents.length > 1) {
    throw new ConfigError(
      "会話を記録する担当は削除できません。先に別のエージェントを担当にしてください",
    );
  }
  const { next, removed } = removeFrom(current, "agents", (a) => (a as { id?: string }).id === id);
  if (removed === 0) throw new ConfigError(`エージェント「${id}」が見つかりません`);

  // 最後の1体を消した場合は「未設定」に戻るだけなので、不変条件は問わない
  const remaining = (getPath(next, "agents") ?? []) as unknown[];
  if (remaining.length > 0) {
    const issues = checkInvariants(next);
    if (issues.length > 0) {
      throw new ConfigError("この削除ではBOTが起動しなくなります", issues);
    }
  }
  const backupPath = await backup(CONFIG_PATH);
  await writeAtomic(CONFIG_PATH, next);
  return { backupPath, applied: [{ path: `agents.${id}`, value: null }], config: next };
}
