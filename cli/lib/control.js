/**
 * 常駐プロセス（run.py）の操作API と話す。
 *
 * 相手は core/control.py。127.0.0.1 固定で、状態を変える操作は POST のみ。
 * ここでもその約束を破らない（GET で副作用を作らない）。
 */

import fs from "node:fs";
import path from "node:path";

/** core/control.py の HOST / DEFAULT_PORT と同じ値 */
export const HOST = "127.0.0.1";
export const DEFAULT_PORT = 8788;

/**
 * config.json の supervisor.port を読む。
 *
 * 読めないときは既定に落とす — 設定がまだ無い（初回）のは**正常な状態**で、
 * ここでエラーを出すと初めて触る人の画面が赤くなる。
 */
export function port(home) {
  try {
    const raw = fs.readFileSync(path.join(home, "config.json"), "utf8");
    const value = JSON.parse(raw)?.supervisor?.port;
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : DEFAULT_PORT;
  } catch {
    return DEFAULT_PORT;
  }
}

function url(home, route) {
  return `http://${HOST}:${port(home)}${route}`;
}

async function request(home, route, method) {
  try {
    const response = await fetch(url(home, route), {
      method,
      signal: AbortSignal.timeout(5000),
    });
    const body = await response.json().catch(() => null);
    return { reachable: true, ok: response.ok, status: response.status, body };
  } catch {
    // 繋がらない = 動いていない。異常ではないので、そう伝えられる形で返す
    return { reachable: false, ok: false, status: 0, body: null };
  }
}

export function status(home) {
  return request(home, "/status", "GET");
}

export function shutdown(home) {
  return request(home, "/shutdown", "POST");
}

/** 止まりきる（ポートが開かなくなる）まで待つ。落ちなければ false */
export async function waitUntilDown(home, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const result = await request(home, "/health", "GET");
    if (!result.reachable) return true;
    if (Date.now() > deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
}

/** 起き上がる（ポートが応えるようになる）まで待つ */
export async function waitUntilUp(home, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const result = await request(home, "/health", "GET");
    if (result.reachable) return true;
    if (Date.now() > deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
}
