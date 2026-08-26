/**
 * `openagents start` / `stop` / `status` — 常駐プロセスの操作。
 */

import path from "node:path";
import { fail, padDisplay, say } from "../ui.js";
import * as control from "../control.js";
import * as git from "../git.js";
import { inherit } from "../proc.js";
import { venvPython } from "../python.js";
import { home, requireCheckout } from "../context.js";

/**
 * 常駐（run.py）を前面で動かす。
 *
 * 動かすのは **venv の Python**。依存はそこに入っているので、
 * システムの Python で起動すると import で落ちる。
 */
export async function start(options) {
  const dir = requireCheckout(home(options));

  const existing = await control.status(dir);
  if (existing.reachable) {
    fail(
      "すでに動いています",
      "  状態を見る:      openagents status\n" +
        "  止める:          openagents stop",
    );
  }

  const python = venvPython(dir);
  if (!python) {
    fail(
      "作業環境（venv）がまだ作られていません",
      "  先にこちらを実行してください:\n\n      openagents setup",
    );
  }

  const code = await inherit(python, [path.join(dir, "run.py")], { cwd: dir });
  return code === 0 ? 0 : 1;
}

export async function stop(options) {
  const dir = requireCheckout(home(options));

  const result = await control.shutdown(dir);
  if (!result.reachable) {
    // 動いていないのは異常ではない。そう伝える
    say("  動いていません。");
    return 0;
  }
  if (!result.ok) {
    fail(
      `止められませんでした（${result.status}）`,
      "  この版の OpenAgents は stop に対応していないかもしれません。\n" +
        "  先に更新してください:\n\n      openagents update",
    );
  }
  const down = await control.waitUntilDown(dir);
  say(down ? "  止めました。" : "  停止要求は届きましたが、まだ終わっていません。");
  return down ? 0 : 1;
}

export async function status(options) {
  const dir = requireCheckout(home(options));
  const version = git.describe(dir);

  say(`  置き場   : ${dir}`);
  say(`  バージョン: ${version || "（git の情報がありません）"}`);

  const result = await control.status(dir);
  if (!result.reachable) {
    say("  常駐     : 動いていません");
    say();
    say("  動かすには: openagents start");
    return 0;
  }
  if (!result.ok || !result.body) {
    say(`  常駐     : 応答が読めません（${result.status}）`);
    return 1;
  }

  say("  常駐     : 動いています");
  say();
  for (const row of serviceRows(result.body)) {
    say(`    ${padDisplay(row.label, 22)} ${row.state}`);
  }
  return 0;
}

/**
 * /status の中身を、そのまま1行ずつ出せる形にする。
 *
 * 相手は core/supervisor.py の Supervisor.status()（`{"services": [...]}`）。
 * **形が違っても例外を投げない** — 表示のためだけの処理で、ここで落ちると
 * 「状態を見たいだけ」の人がスタックトレースを見ることになる。
 */
export function serviceRows(body) {
  const services = body?.services;
  if (!Array.isArray(services)) return [];
  return services.map((service) => ({
    id: service?.id ?? "?",
    label: service?.label || service?.id || "?",
    state: describeState(service),
  }));
}

function describeState(service) {
  if (!service?.enabled) {
    // オフは異常ではない。理由（note）が付いていれば一緒に出す
    return service?.note ? `オフ — ${service.note}` : "オフ";
  }
  if (service.pid) return `動作中  pid ${service.pid}`;
  return service.state || "停止中";
}
