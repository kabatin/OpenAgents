/**
 * ログの末尾取得と追従。
 *
 * 注意点:
 *  - ローテーションは copytruncate（`cp log log.1; : > log`）なので inode は変わらず、
 *    サイズだけが 0 に戻る。**size < offset を検知して先頭へ巻き戻す**必要がある。
 *  - archivebot の bot.log は print 出力でタイムスタンプもレベルも無い。
 *    重要度はキーワードで推定するしかない。
 *  - bot.error.log は discord.py のロガー形式なので構造化されている。
 *    ただし大半は起動ごとの PyNaCl 警告なので、既定では隠す。
 */
import fs from "node:fs";
import fsp from "node:fs/promises";

import { logTargets } from "../paths.ts";

export type LogLevel = "error" | "warn" | "info" | "debug";

export type LogLine = {
  seq: number;
  text: string;
  level: LogLevel;
  agent: string | null;
  timestamp: string | null;
  /** 起動の区切り（「logged in as …」）か */
  boundary: boolean;
  /** 既定では隠す定型ノイズか */
  noisy: boolean;
};

const STRUCTURED_RE = /^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(\w+)\s*\] ([\w.]+): (.*)$/;
// ログ行の先頭にある [エージェントID]。IDは利用者が自由に付けるので、
// 固定の一覧ではなく「IDらしい文字列」として拾う
const AGENT_PREFIX_RE = /^\[([a-z][a-z0-9_-]{0,31})\]\s/;
const DIAG_RE = /^\[診断\]/;
const ERROR_WORDS = /(failed|error|exception|traceback|エラー|失敗|❌)/i;
const WARN_WORDS = /(warn|warning|警告|⚠️|無音|要確認)/i;
const BOUNDARY_RE = /(logged in as|開発BOT logged in)/;
/** 起動のたびに必ず2回出る無害な警告。既定では隠す。 */
const NOISE_RE = /(PyNaCl is not installed|voice will NOT be supported|davey)/i;

export function parseLine(text: string, seq: number): LogLine {
  const structured = STRUCTURED_RE.exec(text);
  if (structured) {
    const [, timestamp, rawLevel, , message] = structured;
    const lvl = (rawLevel ?? "").toUpperCase();
    const level: LogLevel =
      lvl === "ERROR" || lvl === "CRITICAL"
        ? "error"
        : lvl === "WARNING"
          ? "warn"
          : lvl === "DEBUG"
            ? "debug"
            : "info";
    return {
      seq,
      text,
      level,
      agent: null,
      timestamp: timestamp ?? null,
      boundary: false,
      noisy: NOISE_RE.test(message ?? text),
    };
  }

  const agentMatch = AGENT_PREFIX_RE.exec(text);
  const level: LogLevel = ERROR_WORDS.test(text)
    ? "error"
    : WARN_WORDS.test(text)
      ? "warn"
      : "info";
  return {
    seq,
    text,
    level,
    agent: agentMatch?.[1] ?? (DIAG_RE.test(text) ? "devbot" : null),
    timestamp: null,
    boundary: BOUNDARY_RE.test(text),
    noisy: NOISE_RE.test(text),
  };
}

function resolveTarget(id: string): string | null {
  return logTargets().find((t) => t.id === id)?.path ?? null;
}

export type Tail = { lines: LogLine[]; offset: number; size: number };

/** 末尾から指定バイトだけ読む（巨大ログを全部メモリに載せない）。 */
export async function tail(id: string, maxBytes = 64 * 1024): Promise<Tail> {
  const file = resolveTarget(id);
  if (file === null) throw new Error(`不明なログです: ${id}`);
  let size: number;
  try {
    size = (await fsp.stat(file)).size;
  } catch {
    return { lines: [], offset: 0, size: 0 };
  }
  const start = Math.max(0, size - maxBytes);
  const handle = await fsp.open(file, "r");
  try {
    const buf = Buffer.alloc(size - start);
    await handle.read(buf, 0, buf.length, start);
    const text = buf.toString("utf8");
    // 途中から読んだ場合、先頭は行の切れ端なので捨てる
    const body = start > 0 ? text.slice(text.indexOf("\n") + 1) : text;
    const lines = body
      .split("\n")
      .filter((l) => l.length > 0)
      .map((l, i) => parseLine(l, start + i));
    return { lines, offset: size, size };
  } finally {
    await handle.close();
  }
}

export type LogWatcher = { close: () => void };

/**
 * ファイルの追記を監視して新しい行だけをコールバックする。
 * copytruncate でサイズが縮んだら先頭から読み直す。
 */
export function watchLog(
  id: string,
  fromOffset: number,
  onLines: (lines: LogLine[]) => void,
  intervalMs = 1000,
): LogWatcher {
  const file = resolveTarget(id);
  if (file === null) throw new Error(`不明なログです: ${id}`);
  let offset = fromOffset;
  let seq = fromOffset;
  let closed = false;

  const poll = async () => {
    if (closed) return;
    try {
      const size = (await fsp.stat(file)).size;
      if (size < offset) {
        // ローテーション（copytruncate）。同じファイルの先頭に戻る。
        offset = 0;
        onLines([
          {
            seq: seq++,
            text: "--- ログがローテーションされました ---",
            level: "info",
            agent: null,
            timestamp: null,
            boundary: true,
            noisy: false,
          },
        ]);
      }
      if (size > offset) {
        const handle = await fsp.open(file, "r");
        try {
          const buf = Buffer.alloc(Math.min(size - offset, 256 * 1024));
          const { bytesRead } = await handle.read(buf, 0, buf.length, offset);
          offset += bytesRead;
          const lines = buf
            .subarray(0, bytesRead)
            .toString("utf8")
            .split("\n")
            .filter((l) => l.length > 0)
            .map((l) => parseLine(l, seq++));
          if (lines.length > 0) onLines(lines);
        } finally {
          await handle.close();
        }
      }
    } catch {
      // ファイルが一時的に消えていても監視は続ける
    }
  };

  const timer = setInterval(() => void poll(), intervalMs);
  void poll();
  return {
    close: () => {
      closed = true;
      clearInterval(timer);
    },
  };
}

/** ログファイルの一覧とサイズ（肥大の警告に使う）。 */
export async function logInventory(): Promise<
  { id: string; label: string; path: string; sizeBytes: number | null; rotated: boolean }[]
> {
  // rotate-bot-logs.sh が面倒を見ているのは chatbot と meetingbot だけ。
  // devbot のログは対象外なので、それを画面で分かるようにする。
  const rotatedDirs = ["chatbot", "meetingbot"];
  return Promise.all(
    logTargets().map(async (t) => {
      let sizeBytes: number | null = null;
      try {
        sizeBytes = (await fsp.stat(t.path)).size;
      } catch {
        sizeBytes = null;
      }
      return {
        ...t,
        sizeBytes,
        rotated: rotatedDirs.some((d) => t.path.includes(`/${d}/`)),
      };
    }),
  );
}

export const ROTATE_THRESHOLD_BYTES = 50 * 1024 * 1024;

export function existsSync(file: string): boolean {
  return fs.existsSync(file);
}
