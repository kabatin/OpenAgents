/**
 * 設定の読み書きの検証。
 * 「保存で設定を壊さない」ことが最優先なので、往復（読む→1つ変える→書く形にする→読む）
 * で意図した1点以外が完全一致することを確かめる。ファイルへは書き込まない。
 *
 * 対象は同梱のテスト用設定（tests/fixtures/config.test.json）。利用者ごとに
 * 中身が違う実 config.json に依存させないことで、クローン直後でも走る。
 * 同梱の config.example.json が「そのまま起動できる形」かも併せて検査する。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { parseJson } from "../server/config/bigjson.ts";
import { applyPatches, diffJson, getPath } from "../server/config/objpath.ts";
import { CONFIG_EXAMPLE_PATH } from "../server/paths.ts";
import { checkInvariants } from "../server/config/schema.ts";
import { toPatches, PatchError, parseScope } from "../server/config/patcher.ts";
import { flatten, resolveGroups } from "../server/config/catalog.ts";
import { agentGroups } from "../server/config/catalog.agent.ts";
import { DEV_BOT_GROUPS, GLOBAL_GROUPS } from "../server/config/catalog.global.ts";

// 本番と同じ読み方をする（素の JSON.parse は19桁IDを丸めるので使わない）
const FIXTURE = path.join(
  path.dirname(fileURLToPath(import.meta.url)), "fixtures", "config.test.json");
const raw = fs.readFileSync(FIXTURE, "utf8");
const config = parseJson(raw) as Record<string, unknown>;
const agents = config["agents"] as { id: string }[];
const indexOf = (id: string) => agents.findIndex((a) => a.id === id);

describe("設定の読み書き", () => {
  it("テスト用設定は起動できる状態である（不変条件を満たす）", () => {
    expect(checkInvariants(config)).toEqual([]);
  });

  it("同梱の config.example.json はそのままでも壊れていない", () => {
    // token 未設定なので起動はできない。それ以外の不備がゼロであることを見る
    const example = parseJson(fs.readFileSync(CONFIG_EXAMPLE_PATH, "utf8"));
    const issues = checkInvariants(example).filter((i) => !i.path.endsWith("token"));
    expect(issues).toEqual([]);
  });

  it("1つ変えても、それ以外は _comment まで含めて完全一致する", () => {
    // 実ファイルの現在値に依存しないよう、今の値と違う値を選ぶ
    const path = "agents.0.proactive.rest.end_hour";
    const before = getPath(config, path) as number;
    const after = before === 8 ? 7 : 8;
    const patches = toPatches(
      parseScope("agent:agent1"),
      [{ path: "proactive.rest.end_hour", value: after }],
      indexOf,
    );
    const next = applyPatches(config, patches);
    expect(diffJson(config, next)).toEqual([{ path, before, after }]);
  });

  it("書き出して読み直しても構造が保たれる（JSONの往復）", () => {
    const patches = toPatches(
      parseScope("agent:agent2"),
      [{ path: "require_mention", value: false }],
      indexOf,
    );
    const next = applyPatches(config, patches);
    const roundTripped = JSON.parse(JSON.stringify(next, null, 2)) as Record<string, unknown>;
    expect(Array.isArray(roundTripped["agents"])).toBe(true);
    expect((roundTripped["agents"] as unknown[]).length).toBe(agents.length);
    expect(checkInvariants(roundTripped)).toEqual([]);
    expect(diffJson(next, roundTripped)).toEqual([]);
  });

  it("3値トグルは enabled と shadow の2つに展開される", () => {
    const patches = toPatches(
      parseScope("agent:agent1"),
      [{ path: "proactive.rescue", value: "live" }],
      indexOf,
    );
    expect(patches).toEqual([
      { path: "agents.0.proactive.rescue.enabled", value: true },
      { path: "agents.0.proactive.rescue.shadow", value: false },
    ]);
    const next = applyPatches(config, patches);
    expect(checkInvariants(next)).toEqual([]);
  });
});

describe("壊れる変更を拒否する", () => {
  it("アーカイブ保存係が0体になる設定は弾く", () => {
    const broken = applyPatches(config, [{ path: "agents.0.archiver", value: false }]);
    expect(checkInvariants(broken).map((i) => i.message).join()).toContain("ちょうど1体");
  });

  it("アーカイブ保存係が2体になる設定は弾く", () => {
    const broken = applyPatches(config, [{ path: "agents.1.archiver", value: true }]);
    expect(checkInvariants(broken).map((i) => i.message).join()).toContain("ちょうど1体");
  });

  it("アーカイブ保存係のトークンを空にする設定は弾く", () => {
    const broken = applyPatches(config, [{ path: "agents.0.token", value: "" }]);
    expect(checkInvariants(broken).map((i) => i.message).join()).toContain("トークンが空");
  });

  it("agents が配列でなくなったら弾く（配列破壊の最終防衛線）", () => {
    const broken = { ...config, agents: { "0": agents[0] } };
    expect(checkInvariants(broken).length).toBeGreaterThan(0);
  });

  it("開発BOTのトークンを空にする設定は弾く", () => {
    const broken = applyPatches(config, [{ path: "dev_bot.token", value: "" }]);
    expect(checkInvariants(broken).map((i) => i.message).join()).toContain("開発BOTのトークン");
  });
});

describe("画面から触れてはいけないものを拒否する", () => {
  const cases: [string, string][] = [
    ["agent:agent1", "archiver"],
    ["agent:agent1", "home_channel_id"],
    ["global", "guild_id"],
  ];
  for (const [scope, path] of cases) {
    it(`${scope} の ${path} は変更できない`, () => {
      expect(() => toPatches(parseScope(scope), [{ path, value: "x" }], indexOf)).toThrow(
        PatchError,
      );
    });
  }

  it("カタログに無いパスは受け付けない", () => {
    expect(() =>
      toPatches(parseScope("agent:agent1"), [{ path: "proactive.__evil", value: 1 }], indexOf),
    ).toThrow(PatchError);
  });

  it("範囲外の数値は受け付けない", () => {
    expect(() =>
      toPatches(parseScope("agent:agent1"), [{ path: "proactive.rest.end_hour", value: 99 }], indexOf),
    ).toThrow(PatchError);
  });

  it("型が違う値は受け付けない", () => {
    expect(() =>
      toPatches(parseScope("agent:agent1"), [{ path: "skills.reminder", value: "yes" }], indexOf),
    ).toThrow(PatchError);
  });
});

describe("秘密は「書けるが読めない」", () => {
  // セットアップを画面で完結させるにはトークンを保存できる必要がある。
  // ただし読み出しは views 側で必ずマスクする（生値は応答に載せない）。
  const secrets: [string, string][] = [
    ["agent:agent1", "token"],
    ["global", "dev_bot.token"],
    ["meeting", "token"],
  ];
  for (const [scope, path] of secrets) {
    it(`${scope} の ${path} は保存できる`, () => {
      const patches = toPatches(parseScope(scope), [{ path, value: "new-token" }], indexOf);
      expect(patches.length).toBe(1);
      expect(patches[0]?.value).toBe("new-token");
    });
  }

  it("秘密として宣言されている（＝表示時にマスクされる）", () => {
    const agentToken = flatten(agentGroups()).find((s) => s.path === "token");
    expect(agentToken?.secret).toBe(true);
    const devToken = flatten(DEV_BOT_GROUPS).find((s) => s.path === "dev_bot.token");
    expect(devToken?.secret).toBe(true);
  });
});

describe("設定カタログ", () => {
  // 実際に起きた事故の再発防止: bool トグルがオブジェクトのノードを指していると、
  // ON/OFF操作で {enabled, start_hour, ...} が true/false に潰されて設定が消える。
  // 実 config.json に対して「boolトグルの指す先がオブジェクトでない」ことを検査する。
  it("boolトグルが実configのオブジェクトを指していない（トグルで設定を破壊しない）", () => {
    const scopes: [string, unknown][] = [
      ...agents.map((a, i): [string, unknown] => [`agent:${a.id}`, (config["agents"] as unknown[])[i]]),
      ["global", config],
    ];
    const catalogs: [string, ReturnType<typeof flatten>][] = [
      ["agent", flatten(agentGroups())],
      ["global", [...flatten(GLOBAL_GROUPS), ...flatten(DEV_BOT_GROUPS)]],
    ];
    for (const [scopeName, scopeRoot] of scopes) {
      const settings = scopeName === "global" ? catalogs[1]![1] : catalogs[0]![1];
      for (const s of settings) {
        if (s.kind !== "bool" || s.readonly === true) continue;
        const v = getPath(scopeRoot, s.path);
        const isObj = typeof v === "object" && v !== null && !Array.isArray(v);
        expect(isObj, `${scopeName} の ${s.path} がオブジェクトを指している（トグルで破壊される）`).toBe(false);
      }
    }
  });

  it("presenceIsOn は image_gen の有効状態を正しく解決する", () => {
    const idx = indexOf("agent2");
    const withImageGen = (config["agents"] as unknown[])[idx];
    const groups = resolveGroups(agentGroups(), withImageGen, config);
    const row = groups
      .flatMap((g) => g.settings)
      .find((s) => s.path === "skills.image_gen.enabled");
    // agent2 は image_gen オブジェクトを持つ（enabledキー無し）→ ON と解決される
    expect(row?.current.value).toBe(true);
  });

  it("パスが重複していない", () => {
    for (const groups of [agentGroups(), GLOBAL_GROUPS, DEV_BOT_GROUPS]) {
      const paths = flatten(groups).map((s) => s.path);
      expect(new Set(paths).size).toBe(paths.length);
    }
  });

  it("readonly でない設定はすべて画面から保存できる形になっている", () => {
    for (const s of flatten(agentGroups())) {
      if (s.readonly === true || s.kind === "info") continue;
      expect(s.label.length).toBeGreaterThan(0);
      expect(s.desc.length).toBeGreaterThan(0);
    }
  });

  it("_cycle_plan に対応する自発サイクルが漏れなく載っている", () => {
    // agent_loops.py の _cycle_plan() が参照する config キーの一覧。
    // BOT側にサイクルを足したらここも足す（載せ忘れの検出用）。
    const expected = [
      "minutes_channel_id", "weekly_report", "homework", "briefing", "rescue", "prep",
      "profiles", "event_planner", "rule_distill", "selfreview_distill", "stale_watch",
      "pulse", "event_watch",
      "persona_review", "auto_discover", "outreach", "kpi", "demand_watch", "injection_drill",
      "episodes", "ab_test", "org_chart", "persona_checkup", "self_audit", "bias_check",
      "prophecy", "study_group", "news_watch", "newspaper", "ripple", "comeback", "wiki",
    ];
    const paths = flatten(agentGroups()).map((s) => s.path);
    for (const key of expected) {
      const hit = paths.some(
        (p) => p === `proactive.${key}` || p.startsWith(`proactive.${key}.`),
      );
      expect(hit, `proactive.${key} がカタログに無い`).toBe(true);
    }
  });
});

describe("議事録BOTの設定は config.json の一部", () => {
  // 設定ファイルが2枚あると「片方だけ直して動かない」が必ず起きる。
  // meeting スコープの保存先が本体の meeting_bot セクションであることを固定する。
  it("meeting スコープのパスは meeting_bot 配下へ書かれる", () => {
    const patches = toPatches(
      parseScope("meeting"),
      [{ path: "voice_channel_id", value: "123456789012345678" }],
      indexOf,
    );
    // カタログ上はセクション相対。保存時に store が接頭辞を足す
    expect(patches).toEqual([{ path: "voice_channel_id", value: "123456789012345678" }]);
    const next = applyPatches(config, [
      { path: "meeting_bot.voice_channel_id", value: "123456789012345678" },
    ]);
    expect(getPath(next, "meeting_bot.voice_channel_id")).toBe("123456789012345678");
    expect(checkInvariants(next)).toEqual([]);
  });

  it("議事録BOTのトークンは画面から書ける（読み出しはマスクされる）", () => {
    const patches = toPatches(
      parseScope("meeting"),
      [{ path: "token", value: "dummy" }],
      indexOf,
    );
    expect(patches).toEqual([{ path: "token", value: "dummy" }]);
  });
});

describe("LLMの選択", () => {
  it("プロバイダとモデルは画面から変えられる", () => {
    const patches = toPatches(
      parseScope("global"),
      [{ path: "llm.provider", value: "codex" }],
      indexOf,
    );
    const next = applyPatches(config, patches);
    expect(getPath(next, "llm.provider")).toBe("codex");
    expect(checkInvariants(next)).toEqual([]);
  });

  it("選択肢に無いプロバイダは受け付けない", () => {
    expect(() =>
      toPatches(parseScope("global"), [{ path: "llm.provider", value: "でたらめ" }], indexOf),
    ).toThrow(PatchError);
  });

  it("LLM設定を変えても19桁IDは壊れない", () => {
    const before = getPath(config, "guild_id");
    const next = applyPatches(config, [{ path: "llm.model", value: "m2" }]);
    expect(getPath(next, "guild_id")).toBe(before);
  });
});

describe("既定の設定でセットアップが完走できる", () => {
  // 実際に起きた不具合: 開発BOTが「無効・トークン空」でも必須扱いされ、
  // 雛形のままではエージェントを1体も追加できなかった（ウィザードが完走しない）
  it("雛形の設定にエージェントを1体足せる", () => {
    const example = parseJson(fs.readFileSync(CONFIG_EXAMPLE_PATH, "utf8"));
    const blank = applyPatches(example, [
      { path: "guild_id", value: "1234567890123456789" },
      { path: "agents", value: [] },
    ]);
    const withAgent = applyPatches(blank, [
      {
        path: "agents",
        value: [
          {
            id: "agent1",
            name: "エージェント",
            token: "dummy",
            home_channel_id: "1234567890123456780",
            archiver: true,
            persona_files: [],
          },
        ],
      },
    ]);
    expect(checkInvariants(withAgent)).toEqual([]);
  });

  it("開発BOTは有効にしたときだけトークンを要求する", () => {
    const base = applyPatches(config, [{ path: "dev_bot", value: { enabled: false, token: "" } }]);
    expect(checkInvariants(base)).toEqual([]);

    const on = applyPatches(config, [{ path: "dev_bot", value: { enabled: true, token: "" } }]);
    expect(checkInvariants(on).map((i) => i.message).join()).toContain("開発BOTのトークン");
  });

  it("議事録BOTも無効ならトークンを要求しない", () => {
    const off = applyPatches(config, [
      { path: "meeting_bot", value: { enabled: false, token: "" } },
    ]);
    expect(checkInvariants(off)).toEqual([]);
  });
});
