/**
 * 置き場の決まり方。**取り違えると別の会話ログを見に行く**ので、
 * 優先順位はここで固定しておく。
 */

import assert from "node:assert/strict";
import path from "node:path";
import { describe, it } from "node:test";
import {
  DEFAULT_DIR_NAME,
  findCheckoutUpward,
  isCheckout,
  resolveHome,
} from "../lib/home.js";

/** 与えたパスだけが「ある」ことにする exists */
function only(...present) {
  const set = new Set(present.map((p) => path.resolve(p)));
  return (p) => set.has(path.resolve(p));
}

const CHECKOUT = "/work/OpenAgents";
const checkoutExists = only(
  path.join(CHECKOUT, "start.py"),
  path.join(CHECKOUT, "core", "paths.py"),
);

describe("isCheckout", () => {
  it("start.py と core/paths.py が揃っていれば checkout", () => {
    assert.equal(isCheckout(CHECKOUT, checkoutExists), true);
  });

  it("片方だけでは checkout と認めない", () => {
    // start.py だけの同名の別物や、core/ だけを切り出した派生物を拾わないため
    assert.equal(
      isCheckout(CHECKOUT, only(path.join(CHECKOUT, "start.py"))),
      false,
    );
    assert.equal(
      isCheckout(CHECKOUT, only(path.join(CHECKOUT, "core", "paths.py"))),
      false,
    );
  });

  it("空のパスは false", () => {
    assert.equal(isCheckout("", checkoutExists), false);
    assert.equal(isCheckout(null, checkoutExists), false);
  });
});

describe("findCheckoutUpward", () => {
  it("深い場所からでも見つける", () => {
    const deep = path.join(CHECKOUT, "core", "tools");
    assert.equal(findCheckoutUpward(deep, checkoutExists), path.resolve(CHECKOUT));
  });

  it("見つからなければ null（ルートまで行っても止まる）", () => {
    assert.equal(findCheckoutUpward("/somewhere/else", () => false), null);
  });
});

describe("resolveHome", () => {
  const env = {};
  const homedir = "/home/me";

  it("--dir が最優先", () => {
    const got = resolveHome({
      dir: "/explicit",
      env: { OPENAGENTS_HOME: "/from-env" },
      cwd: CHECKOUT,
      homedir,
      exists: checkoutExists,
    });
    assert.equal(got.dir, path.resolve("/explicit"));
    assert.equal(got.source, "--dir");
  });

  it("次に OPENAGENTS_HOME", () => {
    const got = resolveHome({
      env: { OPENAGENTS_HOME: "/from-env" },
      cwd: CHECKOUT,
      homedir,
      exists: checkoutExists,
    });
    assert.equal(got.dir, path.resolve("/from-env"));
    assert.equal(got.source, "OPENAGENTS_HOME");
  });

  it("いま居る場所が checkout ならそこを使う", () => {
    // git clone で入れた人が npm の CLI を後から入れても、
    // ~/.openagents に二重に落ちないための一手
    const got = resolveHome({
      env,
      cwd: path.join(CHECKOUT, "dashboard"),
      homedir,
      exists: checkoutExists,
    });
    assert.equal(got.dir, path.resolve(CHECKOUT));
  });

  it("どれでもなければ ~/.openagents", () => {
    const got = resolveHome({
      env,
      cwd: "/tmp",
      homedir,
      exists: () => false,
    });
    assert.equal(got.dir, path.join(homedir, DEFAULT_DIR_NAME));
    assert.equal(got.source, "既定");
  });

  it("相対パスは cwd を基準に絶対化する", () => {
    const got = resolveHome({
      dir: "./here",
      env,
      cwd: "/base",
      homedir,
      exists: () => false,
    });
    assert.equal(got.dir, path.resolve("/base/here"));
  });
});
