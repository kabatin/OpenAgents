/**
 * archive.db への読み取り専用アクセス。
 *
 * BOTが書き込み続けているWALのDBを外から覗くので、以下を厳守する:
 *   - readonly で開く（万一のバグでもエージェントの状態を壊せない）
 *   - busy_timeout を書き手（db.py の 5000ms）と揃える
 *   - immutable は使わない（WALを無視して古い/壊れた読み取りになる）
 *   - journal_mode / checkpoint / VACUUM は絶対に触らない
 *   - 接続は使い回す（SSEの毎ティックで開き直さない）
 */
import fs from "node:fs";

import Database from "better-sqlite3";

import { ARCHIVE_DB_PATH } from "../paths.ts";

let handle: Database.Database | null = null;
/** 「DBがまだ無い」を1回だけ知らせるためのフラグ */
let announcedMissing = false;

/**
 * 会話のDBはまだ作られていないか。
 *
 * DBは最初のBOT起動時に作られる。セットアップ直後は**まだ無いのが正常**で、
 * ここを異常として扱うと、初めて起動した人の画面がエラーで埋まる。
 */
export function dbMissing(): boolean {
  return !fs.existsSync(ARCHIVE_DB_PATH);
}

export function db(): Database.Database {
  if (handle !== null) return handle;
  const conn = new Database(ARCHIVE_DB_PATH, { readonly: true, fileMustExist: true });
  conn.pragma("busy_timeout = 5000");
  handle = conn;
  return conn;
}

export function closeDb(): void {
  handle?.close();
  handle = null;
  announcedMissing = false;
}

/**
 * 読み取りに失敗しても画面全体を落とさない。
 * DBが一瞬ロックされていた程度で「サービス停止」に見えるのは嘘なので、
 * 呼び出し側は fallback を表示して次のティックで回復させる。
 */
export function safeQuery<T>(fn: (conn: Database.Database) => T, fallback: T): T {
  // まだ会話が1件も記録されていない（＝DBが無い）のは正常。
  // 毎回スタックトレースを出すと、初回起動の画面が真っ赤になって
  // 「壊れている」と思わせてしまう
  if (dbMissing()) {
    if (!announcedMissing) {
      announcedMissing = true;
      console.log(
        "[db] 会話の記録はまだありません（BOTを起動すると作られます）",
      );
    }
    return fallback;
  }
  try {
    return fn(db());
  } catch (error) {
    console.error("[db] 読み取りに失敗しました:", error);
    return fallback;
  }
}

/**
 * DBの時刻表記に合わせた「今日（JST）」の日付文字列。
 * proactive_log.created_at は素のJST文字列（YYYY-MM-DDTHH:MM）で保存されている一方、
 * messages.created_at はUTC+オフセットなので、混ぜて比較しないこと。
 */
export function todayJst(): string {
  return new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
}
