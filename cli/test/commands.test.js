/**
 * コマンド側の、実行を伴わない判断だけを固める。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { hasContents } from "../lib/commands/setup.js";
import { serviceRows } from "../lib/commands/start.js";
import { DEFAULT_PORT, port } from "../lib/control.js";
import { displayWidth, padDisplay } from "../lib/ui.js";

describe("hasContents", () => {
  it("中身があれば true", () => {
    assert.equal(hasContents("/x", () => ["something"]), true);
  });

  it("空なら false（clone してよい）", () => {
    assert.equal(hasContents("/x", () => []), false);
  });

  it("フォルダ自体が無いなら false（clone が作る）", () => {
    assert.equal(
      hasContents("/x", () => {
        throw new Error("ENOENT");
      }),
      false,
    );
  });
});

describe("status の表示", () => {
  // core/supervisor.py の Supervisor.status() が返す形
  const body = {
    services: [
      { id: "archivebot", label: "会話エージェント", enabled: true, pid: 42 },
      { id: "devbot", label: "開発BOT", enabled: false, state: "disabled" },
      {
        id: "meetingbot",
        label: "議事録BOT",
        enabled: false,
        note: "設定がありません",
      },
      { id: "x", label: "起動待ち", enabled: true, pid: null, state: "starting" },
    ],
  };

  it("動いているものは pid を出す", () => {
    assert.equal(serviceRows(body)[0].state, "動作中  pid 42");
  });

  it("オフは異常ではないので、そう出す", () => {
    assert.equal(serviceRows(body)[1].state, "オフ");
  });

  it("オフの理由があれば添える", () => {
    assert.equal(serviceRows(body)[2].state, "オフ — 設定がありません");
  });

  it("有効だが動いていなければ、その状態を出す", () => {
    assert.equal(serviceRows(body)[3].state, "starting");
  });

  it("label が無ければ id で代用する", () => {
    const rows = serviceRows({ services: [{ id: "only-id", enabled: false }] });
    assert.equal(rows[0].label, "only-id");
  });

  it("形が違っても落ちない（状態を見たいだけの人に例外を見せない）", () => {
    for (const bad of [null, undefined, "text", 3, {}, { services: {} }]) {
      assert.deepEqual(serviceRows(bad), [], JSON.stringify(bad));
    }
  });
});

describe("control の port", () => {
  it("設定が無ければ既定（初回は設定が無いのが正常）", () => {
    assert.equal(port("/nonexistent-dir-for-test"), DEFAULT_PORT);
  });

  it("core/control.py と同じ既定値を持つ", () => {
    assert.equal(DEFAULT_PORT, 8788);
  });
});

describe("表示幅", () => {
  it("全角は2つ分として数える", () => {
    assert.equal(displayWidth("会話"), 4);
    assert.equal(displayWidth("bot"), 3);
    assert.equal(displayWidth("開発BOT"), 7);
  });

  it("幅を揃えて詰める（日本語のラベルでも桁が合う）", () => {
    assert.equal(displayWidth(padDisplay("会話", 10)), 10);
    assert.equal(displayWidth(padDisplay("abcdef", 10)), 10);
  });

  it("幅を超えていたら削らない（情報を落とすより崩れる方がまし）", () => {
    assert.equal(padDisplay("あいうえお", 4), "あいうえお");
  });
});
