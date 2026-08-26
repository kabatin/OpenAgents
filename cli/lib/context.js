/**
 * 各コマンドが共通で必要とするもの（置き場・Python）をまとめて用意する。
 *
 * 「置き場が無い」「Python が古い」を各コマンドで別々に書くと、
 * 同じ失敗が別の文言で出てくる。入口をここ1つにする。
 */

import { fail } from "./ui.js";
import { checkoutProblem, resolveHome } from "./home.js";
import { findPython, PYTHON_HOW_TO_FIX, MIN_PYTHON } from "./python.js";

/** 置き場を決める（存在は確かめない。setup はまだ無い状態で呼ぶため） */
export function home(options) {
  return resolveHome({ dir: options.dir, env: process.env });
}

/**
 * 置き場が使える checkout であることを要求する。
 * setup 以外のコマンドは、これを通らないと先へ進めない。
 */
export function requireCheckout(resolved) {
  const problem = checkoutProblem(resolved.dir);
  if (problem === "missing") {
    fail(
      `OpenAgents がまだ入っていません（${resolved.dir} が見つかりません）`,
      "  次で入ります:\n\n      openagents setup\n\n" +
        "  別の場所に入れてある場合は、その場所を指定してください:\n\n" +
        "      openagents status --dir <パス>",
    );
  }
  if (problem === "not-openagents") {
    fail(
      `${resolved.dir} は OpenAgents の置き場ではないようです`,
      "  start.py と core/paths.py が見当たりません。\n" +
        "  --dir か OPENAGENTS_HOME で正しい場所を指定してください。",
    );
  }
  return resolved.dir;
}

/** 条件を満たす Python を要求する。見つからなければ入手先を出して止まる */
export function requirePython() {
  const found = findPython();
  if (found && !found.tooOld) return found;

  const version = MIN_PYTHON.join(".");
  if (found?.tooOld) {
    fail(
      `Python ${version} 以上が必要です（いまは ${found.version.join(".")}）`,
      PYTHON_HOW_TO_FIX,
    );
  }
  fail(`Python が見つかりません（${version} 以上が必要です）`, PYTHON_HOW_TO_FIX);
  return null; // fail() は戻らない
}
