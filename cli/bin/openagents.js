#!/usr/bin/env node
/**
 * OpenAgents の入口。
 *
 * ここは**振り分けだけ**を持つ。判断を書き始めると、コマンドが増えるたびに
 * この1枚が膨らんで、どのコマンドが何をするのか読めなくなる。
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { HELP, parseArgs } from "../lib/args.js";
import { fail, say } from "../lib/ui.js";
import { resolveHome } from "../lib/home.js";
import * as git from "../lib/git.js";
import { setup } from "../lib/commands/setup.js";
import { start, status, stop } from "../lib/commands/start.js";
import { update } from "../lib/commands/update.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function cliVersion() {
  try {
    const raw = fs.readFileSync(path.join(HERE, "..", "package.json"), "utf8");
    return JSON.parse(raw).version || "unknown";
  } catch {
    return "unknown";
  }
}

/**
 * 置き場を出すだけ。
 *
 * 「なぜそこになったか」まで出す — 問い合わせの大半は
 * 「意図しない場所を見ている」で、これ1つで切り分けが終わる。
 */
function where(options) {
  const resolved = resolveHome({ dir: options.dir, env: process.env });
  say(resolved.dir);
  say(`  （${resolved.source} で決まりました）`);
  return 0;
}

function version(options) {
  say(`openagents ${cliVersion()}`);
  const resolved = resolveHome({ dir: options.dir, env: process.env });
  const installed = git.describe(resolved.dir);
  if (installed) say(`本体 ${installed}  (${resolved.dir})`);
  return 0;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));

  if (options.unknown) {
    fail(`知らない指定です: ${options.unknown}`, `  使い方は openagents --help で見られます。`);
  }
  if (options.help || options.command === "help") {
    say(HELP);
    return 0;
  }
  if (options.version || options.command === "version") {
    return version(options);
  }

  switch (options.command) {
    case "setup":
      return setup(options);
    case "start":
      return start(options);
    case "stop":
      return stop(options);
    case "status":
      return status(options);
    case "update":
      return update(options);
    case "where":
      return where(options);
    default:
      say(HELP);
      return 1;
  }
}

main()
  .then((code) => process.exit(code ?? 0))
  .catch((error) => {
    // 想定外はここで一度だけ受ける。スタックトレースを生で見せない
    fail(
      "想定していない失敗が起きました",
      `  ${error?.message || error}\n\n` +
        "  差し支えなければ、この表示を添えて報告してください:\n" +
        "  https://github.com/kabatin/OpenAgents/issues",
    );
  });
