import { describe, expect, it } from "vitest";

import { deletePath, diffJson, getPath, setPath } from "../server/config/objpath.ts";

describe("setPath", () => {
  it("オブジェクトのキーを非破壊で差し替える", () => {
    const before = { a: { b: 1 }, c: 2 };
    const after = setPath(before, "a.b", 9);
    expect(after).toEqual({ a: { b: 9 }, c: 2 });
    expect(before.a.b).toBe(1); // 元は変わらない
  });

  it("既存のキー順を保ち、新規キーは末尾に足す", () => {
    const before = { z: 1, a: 2 };
    expect(Object.keys(setPath(before, "m", 3))).toEqual(["z", "a", "m"]);
  });

  it("コードが読まない未知のキー（_comment）を消さない", () => {
    const before = { _comment: "メモ", enabled: false };
    expect(setPath(before, "enabled", true)).toEqual({ _comment: "メモ", enabled: true });
  });

  // これが config.json 破壊の再発防止。配列がオブジェクトに化けると
  // agent_runtime.py が agents を配列として読めず、全BOTが起動しなくなる。
  it("配列を配列のまま保つ（オブジェクトに化けさせない）", () => {
    const before = { agents: [{ id: "agent1", n: 1 }, { id: "agent2", n: 2 }] };
    const after = setPath(before, "agents.0.n", 99);
    expect(Array.isArray(after.agents)).toBe(true);
    expect(after.agents).toEqual([{ id: "agent1", n: 99 }, { id: "agent2", n: 2 }]);
    expect(before.agents[0]?.n).toBe(1);
  });

  it("配列の深いパスにも潜れる", () => {
    const before = { agents: [{ id: "agent1", proactive: { rest: { end_hour: 7 } } }] };
    const after = setPath(before, "agents.0.proactive.rest.end_hour", 8);
    expect(getPath(after, "agents.0.proactive.rest.end_hour")).toBe(8);
    expect(Array.isArray(after.agents)).toBe(true);
  });

  it("途中が無ければオブジェクトとして作る（配列は勝手に生やさない）", () => {
    expect(setPath({}, "a.b.c", 1)).toEqual({ a: { b: { c: 1 } } });
  });

  it("数字に見えるキーでも、入れ物がオブジェクトならオブジェクトのキーとして扱う", () => {
    const before = { user_mapping: { "193": "<@1>" } };
    const after = setPath(before, "user_mapping.194", "<@2>");
    expect(Array.isArray(after.user_mapping)).toBe(false);
    expect(after.user_mapping).toEqual({ "193": "<@1>", "194": "<@2>" });
  });
});

describe("getPath", () => {
  it("配列の添字を辿れる", () => {
    expect(getPath({ a: [{ b: 5 }] }, "a.0.b")).toBe(5);
  });
  it("無いパスは undefined", () => {
    expect(getPath({ a: 1 }, "a.b.c")).toBeUndefined();
  });
});

describe("deletePath", () => {
  it("キーを消す", () => {
    expect(deletePath({ a: 1, b: 2 }, "b")).toEqual({ a: 1 });
  });
  it("配列の要素そのものは消さない（詰めると別物になるため）", () => {
    const before = { a: [1, 2, 3] };
    expect(deletePath(before, "a.1")).toEqual(before);
  });
});

describe("diffJson", () => {
  it("変化が無ければ空", () => {
    expect(diffJson({ a: 1 }, { a: 1 })).toEqual([]);
  });

  it("値としての配列は「まるごと1つ」として比べる", () => {
    const d = diffJson({ admins: ["1", "2"] }, { admins: ["1"] });
    expect(d).toEqual([{ path: "admins", before: ["1", "2"], after: ["1"] }]);
  });

  it("オブジェクトの配列は要素ごとに潜る", () => {
    const before = { agents: [{ id: "agent1", n: 1 }, { id: "agent2", n: 2 }] };
    const after = { agents: [{ id: "agent1", n: 1 }, { id: "agent2", n: 5 }] };
    expect(diffJson(before, after)).toEqual([
      { path: "agents.1.n", before: 2, after: 5 },
    ]);
  });

  it("キーの追加・削除も差分になる", () => {
    expect(diffJson({ a: 1 }, { a: 1, b: 2 })).toEqual([
      { path: "b", before: undefined, after: 2 },
    ]);
  });
});
