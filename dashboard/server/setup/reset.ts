/**
 * 初期化（危険な操作）。
 *
 * 2段構え:
 *   config … 設定だけ未設定に戻す。会話の記録は残る
 *   all    … 会話の記録（state/）も含めて最初の状態に戻す
 *
 * **消さずに退避する。** 会話のアーカイブは取り直せないので、削除ではなく
 * リネームで逃がす。容量は食うが、押し間違いが取り返しのつかない事故に
 * ならない方を選ぶ。捨てるのは利用者が手でやればよい。
 *
 * 順序も大事:
 *   1. BOTを止める（WALで開かれたまま動かすと壊れた残骸が残る）
 *   2. ダッシュボードが握っているDBハンドルを閉じる（db/ro.ts のキャッシュ）
 *   3. ファイルを退避する
 */
import fsp from "node:fs/promises";
import path from "node:path";

import { closeDb } from "../db/ro.ts";
import {
  APPLIED_DIR,
  ARCHIVE_DB_PATH,
  BACKUPS_DIR,
  CACHE_DIR,
  CONFIG_PATH,
  HEARTBEAT_DIR,
  LOGS_DIR,
  REMINDERS_PATH,
  SERVICES,
} from "../paths.ts";
import { stopViaSupervisor } from "../ops/probes.ts";

export type ResetScope = "config" | "all";

export class ResetError extends Error {}

/** 退避名に使うタイムスタンプ（`2026-08-15T04-11-14-123Z`）。 */
export function stampFor(now: Date): string {
  return now.toISOString().replace(/[:.]/g, "-");
}

/**
 * 「全部消す」で本当に消してよいか、入力された合言葉を検査する（純粋関数）。
 * 取り返しがつかない側だけ、文字を打たせる。
 */
export const CONFIRM_WORD = "初期化";

export function confirmationOk(scope: ResetScope, typed: string): boolean {
  if (scope !== "all") return true;
  return (typed ?? "").trim() === CONFIRM_WORD;
}

/** 消す対象の一覧（純粋関数・画面の説明文とテストで共有する）。 */
export function targetsFor(scope: ResetScope): string[] {
  const config = ["config.json"];
  if (scope === "config") return config;
  return [
    ...config,
    "state/archive.db（会話の記録）",
    "state/reminders.json",
    "state/logs/",
    "state/heartbeat/",
  ];
}

async function exists(p: string): Promise<boolean> {
  try {
    await fsp.access(p);
    return true;
  } catch {
    return false;
  }
}

/** あれば退避する。無ければ何もしない（初回でも失敗させない）。 */
async function moveAside(from: string, to: string, moved: string[]): Promise<void> {
  if (!(await exists(from))) return;
  await fsp.mkdir(path.dirname(to), { recursive: true });
  await fsp.rename(from, to);
  moved.push(path.basename(to));
}

/** BOTを止める。スーパーバイザが落ちていても初期化自体は続ける。 */
async function stopAll(): Promise<void> {
  for (const s of SERVICES) {
    try {
      await stopViaSupervisor(s.id);
    } catch {
      // 動いていない/繋がらないのは「止まっている」と同じ。進めてよい
    }
  }
}

export async function reset(
  scope: ResetScope,
  typed: string,
  now: Date = new Date(),
): Promise<{ moved: string[] }> {
  if (!confirmationOk(scope, typed)) {
    throw new ResetError(`確認のため「${CONFIRM_WORD}」と入力してください`);
  }
  await stopAll();
  closeDb();

  const stamp = stampFor(now);
  const moved: string[] = [];

  await moveAside(CONFIG_PATH, path.join(BACKUPS_DIR, `config.json.${stamp}.json`), moved);

  // 「最後にBOTを起動したときの設定」の控えと、取り直せるキャッシュ。
  // 消した config との差分を取り続けるため、残すと**未適用の変更が大量に
  // 出て、押すとエラーになる**（実際に踏んだ）。次の読み込みで作り直される
  for (const dir of [APPLIED_DIR, CACHE_DIR]) {
    if (await exists(dir)) {
      await fsp.rm(dir, { recursive: true, force: true });
      moved.push(`${path.basename(dir)}/ を削除`);
    }
  }

  if (scope === "config") return { moved };

  // アーカイブは同じ場所にリネームで残す（バックアップだと気づける名前で）
  for (const suffix of ["", "-wal", "-shm"]) {
    await moveAside(
      `${ARCHIVE_DB_PATH}${suffix}`,
      `${ARCHIVE_DB_PATH}.bak-${stamp}${suffix}`,
      moved,
    );
  }
  await moveAside(REMINDERS_PATH, `${REMINDERS_PATH}.bak-${stamp}`, moved);
  // ログと生存証明は取っておく意味が薄いので、そのまま消す
  for (const dir of [LOGS_DIR, HEARTBEAT_DIR]) {
    if (await exists(dir)) {
      await fsp.rm(dir, { recursive: true, force: true });
      moved.push(`${path.basename(dir)}/ を削除`);
    }
  }
  return { moved };
}
