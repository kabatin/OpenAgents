/**
 * Python の見つけ方。実際の実行を伴わない部分だけを固める。
 */

import assert from "node:assert/strict";
import path from "node:path";
import { describe, it } from "node:test";
import {
  MIN_PYTHON,
  candidates,
  meets,
  parseVersion,
  venvPython,
} from "../lib/python.js";

describe("parseVersion", () => {
  it("よくある出力を読む", () => {
    assert.deepEqual(parseVersion("3.12"), [3, 12]);
    assert.deepEqual(parseVersion("3.12.4"), [3, 12]);
    assert.deepEqual(parseVersion("Python 3.10.0"), [3, 10]);
  });

  it("読めなければ null", () => {
    for (const bad of ["", null, undefined, "python", "3"]) {
      assert.equal(parseVersion(bad), null, String(bad));
    }
  });
});

describe("meets", () => {
  it("下限ちょうどは通る", () => {
    assert.equal(meets([3, 10]), true);
  });

  it("古いものは弾く", () => {
    assert.equal(meets([3, 9]), false);
    assert.equal(meets([2, 7]), false);
  });

  it("メジャーが上なら通る（4.0 が出ても弾かない）", () => {
    assert.equal(meets([4, 0]), true);
  });

  it("null は通さない", () => {
    assert.equal(meets(null), false);
  });

  it("start.py の MIN_PYTHON と同じ下限を持つ", () => {
    assert.deepEqual(MIN_PYTHON, [3, 10]);
  });
});

describe("candidates", () => {
  it("Windows では py -3 を最後に試す", () => {
    // python.exe が Microsoft Store のダミーだった場合に、
    // 次の候補へ落ちられるようにしておく
    const names = candidates("win32").map(([command]) => command);
    assert.deepEqual(names, ["python", "python3", "py"]);
    assert.deepEqual(candidates("win32").at(-1)[1], ["-3"]);
  });

  it("mac / Linux は python3 が先", () => {
    assert.deepEqual(
      candidates("darwin").map(([command]) => command),
      ["python3", "python"],
    );
  });
});

describe("venvPython", () => {
  const HOME = "/work/OpenAgents";

  it("Windows の場所を見つける", () => {
    const target = path.join(HOME, "venv", "Scripts", "python.exe");
    assert.equal(venvPython(HOME, (p) => p === target), target);
  });

  it("mac / Linux の場所を見つける", () => {
    const target = path.join(HOME, "venv", "bin", "python");
    assert.equal(venvPython(HOME, (p) => p === target), target);
  });

  it("無ければ null（呼び出し側が setup へ案内する）", () => {
    assert.equal(venvPython(HOME, () => false), null);
  });
});
