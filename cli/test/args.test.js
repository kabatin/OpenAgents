/**
 * 引数の解釈。打ち間違いを黙って別の意味に取らないことが要点。
 *
 * 実行: node --test test/
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { parseArgs } from "../lib/args.js";

describe("parseArgs", () => {
  it("無引数は setup（初めての人が最短で進める側に倒す）", () => {
    assert.equal(parseArgs([]).command, "setup");
  });

  it("サブコマンドを取る", () => {
    for (const name of ["setup", "start", "stop", "status", "update", "where"]) {
      assert.equal(parseArgs([name]).command, name, name);
    }
  });

  it("--restart を拾う", () => {
    const parsed = parseArgs(["update", "--restart"]);
    assert.equal(parsed.command, "update");
    assert.equal(parsed.restart, true);
  });

  it("--dir は値と一緒に取る", () => {
    assert.equal(parseArgs(["status", "--dir", "/tmp/x"]).dir, "/tmp/x");
    assert.equal(parseArgs(["status", "--dir=/tmp/x"]).dir, "/tmp/x");
  });

  it("値の無い --dir は指定漏れとして扱う", () => {
    // 黙って既定に落とすと「指定したのに別の場所が使われた」になる
    assert.ok(parseArgs(["status", "--dir"]).unknown);
    assert.ok(parseArgs(["status", "--dir", "--restart"]).unknown);
    assert.ok(parseArgs(["status", "--dir="]).unknown);
  });

  it("知らないコマンドは打ち間違いとして返す", () => {
    assert.equal(parseArgs(["updat"]).unknown, "updat");
    assert.equal(parseArgs(["--nope"]).unknown, "--nope");
  });

  it("コマンドを2つ並べたら受け付けない", () => {
    assert.equal(parseArgs(["start", "stop"]).unknown, "stop");
  });

  it("--help と --version は単独で成立する", () => {
    assert.equal(parseArgs(["--help"]).help, true);
    assert.equal(parseArgs(["-h"]).help, true);
    assert.equal(parseArgs(["--version"]).version, true);
    // help/version のときは setup に落とさない（意図しない導入を防ぐ）
    assert.equal(parseArgs(["--help"]).command, null);
    assert.equal(parseArgs(["--version"]).command, null);
  });
});
