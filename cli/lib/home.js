/**
 * 「どこの OpenAgents を操作するのか」を決める。
 *
 * このリポジトリは config.json も state/ も venv/ も**リポジトリ直下**に置く
 * 設計なので（core/paths.py の ROOT）、置き場 = リポジトリの clone 先そのもの。
 * ここを取り違えると、別の会話ログを見に行ったり、二重に clone したりする。
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/** 既定の置き場（ホーム直下の隠しフォルダ） */
export const DEFAULT_DIR_NAME = ".openagents";

/**
 * そのフォルダが OpenAgents の checkout か。
 *
 * 目印は2つ揃っていること。start.py だけだと同名の別物を拾いうるし、
 * core/paths.py だけだと core/ を切り出した派生物を拾いうる。
 */
export function isCheckout(dir, exists = fs.existsSync) {
  if (!dir) return false;
  return (
    exists(path.join(dir, "start.py")) &&
    exists(path.join(dir, "core", "paths.py"))
  );
}

/**
 * cwd から上へ辿って checkout を探す。見つからなければ null。
 *
 * これがあると、**git clone で入れた人が npm の CLI を後から入れても、
 * 自分の checkout がそのまま操作対象になる**（~/.openagents に二重に落ちない）。
 */
export function findCheckoutUpward(startDir, exists = fs.existsSync) {
  let dir = path.resolve(startDir);
  for (;;) {
    if (isCheckout(dir, exists)) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null; // ルートまで来た
    dir = parent;
  }
}

/**
 * 置き場を決める。純粋関数（依存は引数で渡す）。
 *
 * 戻り値の `source` は、なぜそこになったかの説明用。
 * 「意図しない場所が使われている」の切り分けが、これだけで済む。
 */
export function resolveHome({
  dir = null,
  env = {},
  cwd = process.cwd(),
  homedir = os.homedir(),
  exists = fs.existsSync,
} = {}) {
  if (dir) {
    return { dir: path.resolve(cwd, dir), source: "--dir" };
  }
  const fromEnv = env.OPENAGENTS_HOME;
  if (fromEnv) {
    return { dir: path.resolve(cwd, fromEnv), source: "OPENAGENTS_HOME" };
  }
  const found = findCheckoutUpward(cwd, exists);
  if (found) {
    return { dir: found, source: "いま居る場所" };
  }
  return { dir: path.join(homedir, DEFAULT_DIR_NAME), source: "既定" };
}

/**
 * 置き場が使える状態であることを確かめ、駄目なら理由を返す。
 * （fail() を呼ばず値で返すのは、呼び出し側で文言を変えたいことがあるため）
 */
export function checkoutProblem(dir, exists = fs.existsSync) {
  if (!exists(dir)) return "missing";
  if (!isCheckout(dir, exists)) return "not-openagents";
  return null;
}
