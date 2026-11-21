/**
 * 「頼まれているか」の集計と、サーバーが返す範囲。
 * 質問文・脚注（発言の中身が入りうる）を返さないことと、Bot 起点のターンを
 * 数えないことを固定する。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import Database from "better-sqlite3";
import { afterAll, describe, expect, it, vi } from "vitest";

import {
  categoryOf,
  summarizeRequests,
  type RequestRow,
} from "../web/lib/requests.ts";
import { recentWeeks, weekStart } from "../web/lib/weeks.ts";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "oa-req-"));
const dbPath = path.join(dir, "archive.db");

vi.mock("../server/paths.ts", async (orig) => ({
  ...(await orig<typeof import("../server/paths.ts")>()),
  ARCHIVE_DB_PATH: dbPath,
}));

// 2026-11-10 は火曜。この週の月曜は 11/9
const TODAY = "2026-11-10";

const row = (
  day: string | null,
  action: string,
  tools = "",
  who: string | null = "利用者A",
): RequestRow => ({ day, action, tools, who });

describe("週の区切り", () => {
  it("月曜始まり。日付として読めなければ null", () => {
    expect(weekStart("2026-11-10")).toBe("2026-11-09");
    expect(weekStart("2026-11-15")).toBe("2026-11-09");
    expect(weekStart("x")).toBeNull();
    expect(weekStart(null)).toBeNull();
  });

  it("新しい週が先", () => {
    expect(recentWeeks(TODAY, 2)).toEqual(["2026-11-09", "2026-11-02"]);
    expect(recentWeeks(TODAY, 0)).toEqual([]);
  });
});

describe("頼まれごとの種類", () => {
  it("道具を使わなかった返事は「会話だけ」", () => {
    expect(categoryOf("")).toBe("none");
  });

  it("調べものを挟んでも、やった操作のほうで数える", () => {
    expect(categoryOf("list_reminders,search_messages,add_reminder")).toBe(
      "reminder",
    );
    expect(categoryOf("list_tasks,update_task")).toBe("task");
  });

  it("調べただけなら「過去の会話」", () => {
    expect(categoryOf("search_messages,get_facts,get_decisions")).toBe("recall");
    expect(categoryOf("list_tasks")).toBe("recall");
  });

  it("道具の系統ごとに分かれる", () => {
    expect(categoryOf("move_reminder")).toBe("reminder");
    expect(categoryOf("save_glossary,save_term")).toBe("teach");
    expect(categoryOf("cancel_rule")).toBe("teach");
    expect(categoryOf("cancel_fact")).toBe("teach");
  });

  it("連携プラグインなど、知らない道具は「その他」", () => {
    expect(categoryOf("brand_new_tool")).toBe("other");
    expect(categoryOf("search_messages,brand_new_tool")).toBe("other");
  });
});

describe("頼まれごとの集計", () => {
  it("頼まれた数と、道具を使って応えた数を週ごとに数える", () => {
    const s = summarizeRequests(
      [
        row("2026-11-09", "used", "add_reminder"),
        row("2026-11-10", "unused"),
        row("2026-11-05", "used", "search_messages", "利用者B"),
      ],
      TODAY,
      2,
    );
    expect(s.weeks).toEqual(["2026-11-09", "2026-11-02"]);
    expect(s.byWeek).toEqual([
      { asked: 2, acted: 1, people: 1 },
      { asked: 1, acted: 1, people: 1 },
    ]);
    expect(s.total).toEqual({ asked: 3, acted: 2, people: 2 });
  });

  it("シャドーの試し書きは数えない（同じ依頼を二重に数えない）", () => {
    const s = summarizeRequests(
      [row("2026-11-09", "shadow", "search_messages")],
      TODAY,
    );
    expect(s.total.asked).toBe(0);
  });

  it("範囲外の週と、日付の読めない行は数えない", () => {
    const s = summarizeRequests(
      [row("2026-09-01", "used", "save_fact"), row(null, "used", "save_fact")],
      TODAY,
      4,
    );
    expect(s.total.asked).toBe(0);
    expect(s.categories).toEqual([]);
  });

  it("種類は多い順、週の並びは weeks と同じ", () => {
    const s = summarizeRequests(
      [
        row("2026-11-09", "used", "add_reminder"),
        row("2026-11-03", "used", "list_reminders"),
        row("2026-11-03", "used", "save_fact"),
      ],
      TODAY,
      2,
    );
    expect(s.categories).toEqual([
      { id: "reminder", asked: 2, byWeek: [1, 1] },
      { id: "teach", asked: 1, byWeek: [0, 1] },
    ]);
  });

  it("頼んだ人は多い順。名前の分からない人は人数に入れない", () => {
    const s = summarizeRequests(
      [
        row("2026-11-09", "used", "save_fact", "利用者A"),
        row("2026-11-09", "used", "save_fact", "利用者A"),
        row("2026-11-09", "unused", "", "利用者B"),
        row("2026-11-09", "unused", "", null),
      ],
      TODAY,
      1,
    );
    expect(s.people).toEqual([
      { name: "利用者A", asked: 2 },
      { name: "利用者B", asked: 1 },
    ]);
    expect(s.total).toEqual({ asked: 4, acted: 2, people: 2 });
  });

  it("週数が0以下・今日が読めないときは空", () => {
    expect(summarizeRequests([row("2026-11-09", "used")], TODAY, 0).weeks).toEqual(
      [],
    );
    expect(summarizeRequests([row("2026-11-09", "used")], "x").total.asked).toBe(0);
  });
});

{
  const w = new Database(dbPath);
  w.exec(`CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, display_name TEXT,
            is_bot INTEGER DEFAULT 0);
          CREATE TABLE messages (id INTEGER PRIMARY KEY, author_id INTEGER, content TEXT);
          CREATE TABLE proactive_log (id INTEGER PRIMARY KEY, created_at TEXT,
            agent_id TEXT, kind TEXT, action TEXT, channel_id INTEGER,
            trigger_message_id INTEGER, posted_message_id INTEGER, detail TEXT);`);
  w.prepare("INSERT INTO users VALUES (1, 'user_a', '利用者A', 0)").run();
  w.prepare("INSERT INTO users VALUES (2, 'user_b', NULL, 0)").run();
  w.prepare("INSERT INTO users VALUES (9, 'bot', 'ほかのBOT', 1)").run();
  w.prepare("INSERT INTO messages VALUES (10, 1, '内緒の質問文')").run();
  w.prepare("INSERT INTO messages VALUES (11, 2, 'x')").run();
  w.prepare("INSERT INTO messages VALUES (12, 9, 'x')").run();
  const add = w.prepare(
    `INSERT INTO proactive_log (created_at, agent_id, kind, action,
       trigger_message_id, detail) VALUES (?, 'agent1', ?, ?, ?, ?)`,
  );
  const now = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 16);
  add.run(now, "tool_loop", "used", 10, "save_glossary | -# 📖 単語帳に登録: 「内緒」");
  add.run(now, "tool_loop", "unused", 11, null);
  add.run(now, "tool_loop", "used", 12, "search_messages"); // Bot 起点
  add.run(now, "rescue", "spoke", 10, "x"); // 別の種類の記録
  add.run("2020-01-01T00:00", "tool_loop", "used", 10, "search_messages"); // 古すぎ
  w.close();
}

const { requestLog, toolsOf } = await import("../server/db/queries.requests.ts");
const { closeDb } = await import("../server/db/ro.ts");

afterAll(() => {
  closeDb();
  fs.rmSync(dir, { recursive: true, force: true });
});

describe("サーバーが返す道具の一覧", () => {
  it("脚注（発言の中身が入りうる）は落とす", () => {
    expect(
      toolsOf("save_glossary,save_term | -# 📖 単語帳に登録: 「水野」→「ミズノ」"),
    ).toBe("save_glossary,save_term");
    expect(toolsOf("search_messages")).toBe("search_messages");
    expect(toolsOf(null)).toBe("");
  });
});

describe("頼まれた回答の記録", () => {
  it("人が頼んだ回答だけを返す。Bot 起点・別の種類・古い記録は入らない", () => {
    const { rows } = requestLog();
    expect(rows.map((r) => [r.action, r.tools, r.who])).toEqual([
      ["used", "save_glossary", "利用者A"],
      ["unused", "", "user_b"],
    ]);
  });

  it("質問文も脚注も返さない", () => {
    const text = JSON.stringify(requestLog());
    expect(text).not.toContain("内緒");
  });
});
