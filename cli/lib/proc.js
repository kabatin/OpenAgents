/**
 * 外部コマンドの実行。
 *
 * shell は**使わない**。パスに空白の入るフォルダ（"My Documents" など）と
 * Windows のクォート規則の組み合わせで、静かに別のものを実行してしまうため。
 */

import { spawn, spawnSync } from "node:child_process";

/** 実行して出力を受け取る。失敗しても例外にはしない（判断は呼び出し側） */
export function capture(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    shell: false,
    ...options,
  });
  return {
    ok: result.status === 0,
    code: result.status,
    stdout: (result.stdout || "").trim(),
    stderr: (result.stderr || "").trim(),
    error: result.error || null,
  };
}

/** そのコマンドが実行できるか（--version が通るか）だけを見る */
export function canRun(command, args = ["--version"]) {
  const result = capture(command, args, { timeout: 15000 });
  return result.ok;
}

/**
 * 画面を渡して実行し、終了コードを返す。
 *
 * start.py / run.py はどちらも対話的に出力するので、
 * 出力を横取りせずそのまま流す。
 */
export function inherit(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      stdio: "inherit",
      shell: false,
      ...options,
    });
    child.on("error", () => resolve(-1));
    child.on("close", (code) => resolve(code ?? 0));
  });
}

/**
 * 親から切り離して起動する（この CLI が終わっても動き続ける）。
 *
 * `update --restart` で常駐を入れ替えるときに使う。出力は捨てる —
 * 常駐プロセスは自分で state/logs/ に書くので、ここで拾う必要がない。
 */
export function detach(command, args, options = {}) {
  const child = spawn(command, args, {
    stdio: "ignore",
    shell: false,
    detached: true,
    ...options,
  });
  child.unref();
  return child.pid ?? null;
}
