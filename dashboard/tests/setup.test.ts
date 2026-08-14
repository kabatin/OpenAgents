/**
 * セットアップまわりの検証。
 *
 * ここで守りたいのは3つ:
 *   1. ファイル書き込みが personas/ と knowledge/ の外へ出ないこと
 *   2. トークンが応答に漏れないこと
 *   3. エージェントの増減で設定が壊れないこと
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { appendTo, getPath, removeFrom, setPath } from "../server/config/objpath.ts";
import { maskSecret } from "../server/config/catalog.ts";
import { parseFrontMatter, PersonaError, resolveSafe } from "../server/setup/personas.ts";
import { inviteUrl } from "../server/setup/discord.ts";
import { PERSONAS_DIR } from "../server/paths.ts";

describe("性格ファイルの書き込み先を閉じ込める", () => {
  // パスを受け取ってファイルを書くAPIは、`../` でどこにでも書ける事故の定番。
  const evil = [
    "../../.ssh/authorized_keys",
    "../config.json",
    "..\\..\\config.json",
    "sub/dir.md",
    "/etc/passwd.md",
    "../personas/x.md",
  ];
  for (const name of evil) {
    it(`拒否する: ${name}`, () => {
      expect(() => resolveSafe("personas", name)).toThrow(PersonaError);
    });
  }

  it("拡張子が .md でなければ拒否する", () => {
    expect(() => resolveSafe("personas", "agent1.py")).toThrow(PersonaError);
    expect(() => resolveSafe("personas", "agent1")).toThrow(PersonaError);
  });

  it("空のファイル名を拒否する", () => {
    expect(() => resolveSafe("personas", "")).toThrow(PersonaError);
  });

  it("普通の名前は personas/ の直下に解決される", () => {
    const full = resolveSafe("personas", "agent1.md");
    expect(path.dirname(full)).toBe(path.resolve(PERSONAS_DIR));
    expect(path.basename(full)).toBe("agent1.md");
  });

  it("知らない場所は拒否する", () => {
    // @ts-expect-error わざと不正な area を渡す
    expect(() => resolveSafe("etc", "x.md")).toThrow(PersonaError);
  });
});

describe("フロントマターの解釈", () => {
  it("name / summary / placeholders を読む", () => {
    const { meta, body } = parseFrontMatter(
      "---\nname: 丁寧なアシスタント\nsummary: 敬語で答える\nplaceholders: [AGENT_NAME, TEAM_NAME]\n---\n\n本文です\n",
    );
    expect(meta["name"]).toBe("丁寧なアシスタント");
    expect(meta["summary"]).toBe("敬語で答える");
    expect(meta["placeholders"]).toEqual(["AGENT_NAME", "TEAM_NAME"]);
    expect(body.trim()).toBe("本文です");
  });

  it("フロントマターが無ければ全部が本文", () => {
    const { meta, body } = parseFrontMatter("# ただの見出し\n");
    expect(meta).toEqual({});
    expect(body).toBe("# ただの見出し\n");
  });

  it("閉じていないフロントマターでも壊れない", () => {
    const { body } = parseFrontMatter("---\nname: x\n本文\n");
    expect(body).toContain("本文");
  });

  it("同梱テンプレートはすべて読める", () => {
    const files = fs
      .readdirSync(PERSONAS_DIR)
      .filter((f) => f.endsWith(".template.md"));
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      const { meta } = parseFrontMatter(fs.readFileSync(path.join(PERSONAS_DIR, f), "utf8"));
      expect(meta["name"], `${f} に name がない`).toBeTruthy();
      expect(meta["summary"], `${f} に summary がない`).toBeTruthy();
    }
  });
});

describe("秘密が画面に出ない", () => {
  it("トークンはマスクされる", () => {
    // Discordトークンと同じ「3つの部分」の形にした偽物（本物ではない）。
    // シークレット検出ツールに拾われないよう、それと分かる文字列にしてある
    const fake = ["NOT-A-REAL-TOKEN", "FAKE-MIDDLE", "FAKE-SIGNATURE-PART"].join(".");
    const masked = maskSecret(fake);
    expect(masked).not.toContain("FAKE-SIGNATURE-PART");
    expect(masked).not.toContain("FAKE-MIDDLE");
  });

  it("未設定は未設定と分かる", () => {
    expect(maskSecret("")).toBeTruthy();
    expect(maskSecret(undefined)).toBeTruthy();
  });
});

describe("招待URL", () => {
  it("必要な権限とスコープを含む", () => {
    const url = new URL(inviteUrl("123456789"));
    expect(url.searchParams.get("client_id")).toBe("123456789");
    expect(url.searchParams.get("scope")).toBe("bot");
    // 管理者権限(1<<3)を要求していないこと（過剰な権限は求めない）
    const perms = BigInt(url.searchParams.get("permissions") ?? "0");
    expect(perms & (1n << 3n)).toBe(0n);
    // メッセージ送信は含む
    expect(perms & (1n << 11n)).not.toBe(0n);
  });
});

describe("配列の増減", () => {
  const base = { agents: [{ id: "a1" }, { id: "a2" }] };

  it("末尾に足せる", () => {
    const next = appendTo(base, "agents", { id: "a3" });
    expect((getPath(next, "agents") as unknown[]).length).toBe(3);
    // 元は変わらない
    expect((getPath(base, "agents") as unknown[]).length).toBe(2);
  });

  it("配列が無ければ作る", () => {
    const next = appendTo({}, "agents", { id: "a1" });
    expect(getPath(next, "agents")).toEqual([{ id: "a1" }]);
  });

  it("一致条件で消す（添字ではなく）", () => {
    const { next, removed } = removeFrom(base, "agents", (a) => (a as { id: string }).id === "a1");
    expect(removed).toBe(1);
    expect(getPath(next, "agents")).toEqual([{ id: "a2" }]);
  });

  it("一致しなければ何も消さない", () => {
    const { next, removed } = removeFrom(base, "agents", (a) => (a as { id: string }).id === "zz");
    expect(removed).toBe(0);
    expect(getPath(next, "agents")).toEqual(base.agents);
  });

  it("配列でない場所を消そうとしても壊れない", () => {
    const { removed } = removeFrom({ x: 1 }, "x", () => true);
    expect(removed).toBe(0);
  });

  it("19桁IDを持つ要素を足しても丸まらない", () => {
    const withId = setPath({}, "agents", []);
    const next = appendTo(withId, "agents", { home_channel_id: "1234567890123456789" });
    expect(getPath(next, "agents.0.home_channel_id")).toBe("1234567890123456789");
  });
});
