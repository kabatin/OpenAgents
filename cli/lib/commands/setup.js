/**
 * `openagents setup` — 入れて、設定画面を開くところまで。
 *
 * **この CLI は入口を作るだけ。** venv の作成も依存の導入も画面の組み立ても
 * start.py がすでに持っているので、こちらでは一切やらない。
 */

import fs from "node:fs";
import path from "node:path";
import { fail, heading, say } from "../ui.js";
import { isCheckout } from "../home.js";
import * as git from "../git.js";
import { inherit } from "../proc.js";
import { home, requirePython } from "../context.js";

export async function setup(options) {
  const resolved = home(options);
  heading("OpenAgents をはじめます");

  if (isCheckout(resolved.dir)) {
    say(`  すでに入っています: ${resolved.dir}`);
  } else {
    installInto(resolved.dir);
  }

  const python = requirePython();
  say();
  say("  設定画面を開きます（ここから先は画面の案内どおりに進めてください）");
  say();

  // 以降の検査（証明書・Node・依存・画面の組み立て）は start.py に任せる
  const code = await inherit(
    python.command,
    [...python.args, path.join(resolved.dir, "start.py")],
    { cwd: resolved.dir },
  );
  return code === 0 ? 0 : 1;
}

/** 中身のあるフォルダか。無いフォルダは「空」と同じ扱い（clone が作る） */
export function hasContents(dir, readdir = fs.readdirSync) {
  try {
    return readdir(dir).length > 0;
  } catch {
    return false;
  }
}

function installInto(dir) {
  if (!git.hasGit()) {
    fail(
      "git が見つかりません（更新にも使うので必要です）",
      "  mac:      xcode-select --install\n" +
        "  Windows:  https://git-scm.com/download/win\n" +
        "  Linux:    お使いのパッケージ管理から git を入れてください",
    );
  }

  // 空でないフォルダへの clone は git が失敗する。理由が分かる形で先に止める
  if (hasContents(dir)) {
    fail(
      `${dir} には別のものが入っています`,
      "  中身を確かめて、消すか、別の場所を指定してください:\n\n" +
        "      openagents setup --dir <別のパス>",
    );
  }

  say(`  ${dir} に入れています（初回は数分かかります）…`);
  say();
  const result = git.clone(git.REPO_URL, dir);
  if (!result.ok) {
    const tail = result.stderr
      ? `  ${result.stderr.split("\n").slice(-8).join("\n  ")}\n\n`
      : "";
    fail(
      "取得に失敗しました",
      tail +
        "  ネットワークに繋がっているか、プロキシの設定が要らないかを確認してください。",
    );
  }
}
