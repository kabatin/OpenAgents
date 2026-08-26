/**
 * git の薄い包み。
 *
 * 更新を git に任せているのは、git clone で入れた既存利用者と
 * npm で入れた人を**同じ1本の経路**に載せるため。npm 側に独自の
 * 差分配布を持つと、経路が2本になって「どちらで入れたか」で挙動が変わる。
 */

import fs from "node:fs";
import path from "node:path";
import { capture } from "./proc.js";

export const REPO_URL = "https://github.com/kabatin/OpenAgents.git";

export function hasGit() {
  return capture("git", ["--version"], { timeout: 15000 }).ok;
}

/** そのフォルダが git の作業ツリーか（zip で落とした人はここが false） */
export function isWorkTree(dir, exists = fs.existsSync) {
  return exists(path.join(dir, ".git"));
}

export function clone(url, dir) {
  return capture("git", ["clone", url, dir], { stdio: "inherit" });
}

export function head(dir) {
  const result = capture("git", ["-C", dir, "rev-parse", "HEAD"]);
  return result.ok ? result.stdout : null;
}

/** 直近のタグ（無ければ短縮ハッシュ）。status / --version の表示に使う */
export function describe(dir) {
  const tagged = capture("git", ["-C", dir, "describe", "--tags", "--always"]);
  return tagged.ok ? tagged.stdout : null;
}

/**
 * 追跡中のファイルへの、コミットされていない変更（空なら綺麗）。
 *
 * **未追跡ファイルは数えない**（--untracked-files=no）。置き場には
 * ログや書き出しなど追跡外のものが普通に転がっていて、それで更新を
 * 止めると「毎回止まる」ようになる。未追跡が衝突する稀な場合は
 * git pull 自身が何も壊さずに拒否するので、そちらに任せてよい。
 */
export function dirtyFiles(dir) {
  const result = capture("git", [
    "-C",
    dir,
    "status",
    "--short",
    "--untracked-files=no",
  ]);
  if (!result.ok || !result.stdout) return [];
  return result.stdout.split("\n").filter(Boolean);
}

/**
 * 早送りだけの更新。
 *
 * **--ff-only は意図的**。勝手に merge やリベースをすると、
 * 手元で直した設定やペルソナが競合して、更新のたびに解決を迫られる。
 * 早送りできないなら、それは人間が判断すべき状態。
 */
export function pull(dir) {
  return capture("git", ["-C", dir, "pull", "--ff-only"], { timeout: 300000 });
}

/** old..new の1行ログ。更新で何が変わったかを見せるため */
export function logBetween(dir, from, to, limit = 15) {
  const result = capture("git", [
    "-C",
    dir,
    "log",
    "--oneline",
    `--max-count=${limit}`,
    `${from}..${to}`,
  ]);
  if (!result.ok || !result.stdout) return [];
  return result.stdout.split("\n").filter(Boolean);
}

/** from..to で変更されたファイル一覧（依存の入れ直しが要るかの判断に使う） */
export function changedFiles(dir, from, to) {
  const result = capture("git", ["-C", dir, "diff", "--name-only", from, to]);
  if (!result.ok || !result.stdout) return [];
  return result.stdout.split("\n").filter(Boolean);
}
