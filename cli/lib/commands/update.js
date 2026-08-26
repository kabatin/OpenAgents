/**
 * `openagents update` — 最新に更新する。
 *
 * 依存の入れ直しと画面の組み立ては start.py の ensure_* をそのまま呼ぶ。
 * requirements.txt のハッシュ（start.py の ensure_python_deps）と
 * ソースの更新時刻（同 _dashboard_needs_build）で、必要なときだけ動く判断は
 * すでに向こうにある。**同じ判断をこちらにも書くと、いつか食い違う。**
 */

import path from "node:path";
import { fail, say } from "../ui.js";
import * as control from "../control.js";
import * as git from "../git.js";
import { capture, detach } from "../proc.js";
import { venvPython } from "../python.js";
import { home, requireCheckout, requirePython } from "../context.js";

/** start.py の準備処理だけを呼ぶ（画面は開かない） */
const PREPARE_SNIPPET =
  "import start; start.check_python(); " +
  "p = start.ensure_venv(); start.ensure_python_deps(p); start.ensure_dashboard()";

export async function update(options) {
  const dir = requireCheckout(home(options));

  if (!git.isWorkTree(dir)) {
    fail(
      `${dir} は git で取得したものではないため、更新できません`,
      "  zip などで入れた場合は、いちど git で入れ直すのが確実です:\n\n" +
        "      openagents setup --dir <新しいパス>",
    );
  }

  const dirty = git.dirtyFiles(dir);
  if (dirty.length > 0) {
    // **勝手に stash しない。** 手元の変更を消したように見える更新は、
    // 一度でもやると二度と信用されない
    fail(
      "手元に変更があるため、更新を見送りました",
      `  ${dirty.slice(0, 20).join("\n  ")}\n\n` +
        "  変更を残すなら退避を、要らないなら破棄をしてから、もう一度実行してください:\n\n" +
        `      git -C ${dir} stash        # 退避する\n` +
        `      git -C ${dir} checkout .   # 破棄する`,
    );
  }

  const before = git.head(dir);
  say("  更新を確認しています…");
  const pulled = git.pull(dir);
  if (!pulled.ok) {
    fail(
      "更新の取得に失敗しました",
      (pulled.stderr ? `  ${pulled.stderr.split("\n").slice(-8).join("\n  ")}\n\n` : "") +
        "  ネットワークに繋がっているかを確認してください。",
    );
  }

  const after = git.head(dir);
  if (before && after && before === after) {
    say(`  すでに最新です（${git.describe(dir) || after.slice(0, 7)}）`);
    return 0;
  }

  say();
  say(`  更新しました: ${git.describe(dir) || (after || "").slice(0, 7)}`);
  const commits = git.logBetween(dir, before, after);
  if (commits.length > 0) {
    say();
    for (const line of commits) say(`    ${line}`);
  }

  say();
  say("  必要なものを入れ直しています…");
  const python = requirePython();
  const prepared = capture(python.command, [...python.args, "-c", PREPARE_SNIPPET], {
    cwd: dir,
    timeout: 900000,
  });
  if (!prepared.ok) {
    const detail = `${prepared.stdout}\n${prepared.stderr}`.trim();
    fail(
      "更新後の準備に失敗しました",
      (detail ? `  ${detail.split("\n").slice(-15).join("\n  ")}\n\n` : "") +
        `  コードの更新自体は済んでいます。次で最初からやり直せます:\n\n` +
        "      openagents setup",
    );
  }

  return options.restart ? await restart(dir) : announce(dir);
}

async function announce(dir) {
  const running = await control.status(dir);
  say();
  if (running.reachable) {
    say("  更新を反映するには、常駐プロセスを入れ替えてください:");
    say();
    say("      openagents update --restart");
    say();
    say("  （いま動いているものは、古いコードのままです）");
  } else {
    say("  完了しました。次で動かせます:  openagents start");
  }
  return 0;
}

/**
 * 常駐を入れ替える。
 *
 * 個々のBOTの restart では足りない — run.py 自身が古い supervisor.py を
 * 抱えたままになる。プロセスごと立て直す必要がある。
 */
async function restart(dir) {
  const running = await control.status(dir);
  if (!running.reachable) {
    say();
    say("  常駐は動いていなかったので、そのまま起動します。");
    return launch(dir);
  }

  say();
  say("  常駐プロセスを入れ替えています…");
  const result = await control.shutdown(dir);
  if (!result.reachable || !result.ok) {
    fail(
      "動いている常駐プロセスを止められませんでした",
      "  更新前の版は stop に対応していないことがあります。\n" +
        "  その常駐プロセスを Ctrl-C で止めてから、次を実行してください:\n\n" +
        "      openagents start",
    );
  }
  if (!(await control.waitUntilDown(dir))) {
    fail(
      "常駐プロセスが止まりきりませんでした",
      "  手で止めてから、openagents start を実行してください。",
    );
  }
  return launch(dir);
}

async function launch(dir) {
  const python = venvPython(dir);
  if (!python) {
    fail(
      "作業環境（venv）が見つかりません",
      "  openagents setup を実行してください。",
    );
  }
  detach(python, [path.join(dir, "run.py")], { cwd: dir });
  const up = await control.waitUntilUp(dir);
  say(up ? "  入れ替えました。" : "  起動を確認できませんでした。ログを見てください。");
  return up ? 0 : 1;
}
