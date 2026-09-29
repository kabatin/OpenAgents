/**
 * Discord の ID（19桁）は JS の number に収まらない。数値のまま返すと丸められ、
 * 「Discordで開く」が別の投稿を指していた。SQL 側で文字列にして返すことを固定する。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import Database from "better-sqlite3";
import { afterAll, describe, expect, it, vi } from "vitest";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "oa-ids-"));
const dbPath = path.join(dir, "archive.db");

vi.mock("../server/paths.ts", async (orig) => ({
  ...(await orig<typeof import("../server/paths.ts")>()),
  ARCHIVE_DB_PATH: dbPath,
}));

// 丸めが起きる実在の桁数（Number.MAX_SAFE_INTEGER を超える）
const CH = "1522544734021619764";
const MSG = "1522544734021619771";
const TRIG = "1522544734021619700";

{
  const w = new Database(dbPath);
  w.exec(`CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT);
          CREATE TABLE users (id INTEGER PRIMARY KEY, display_name TEXT);
          CREATE TABLE messages (id INTEGER PRIMARY KEY, author_id INTEGER, content TEXT);
          CREATE TABLE proactive_log (id INTEGER PRIMARY KEY, created_at TEXT,
            agent_id TEXT, kind TEXT, action TEXT, channel_id INTEGER,
            trigger_message_id INTEGER, posted_message_id INTEGER, detail TEXT);`);
  // きっかけの発言と、実際に投稿した本文（タイムラインで冒頭を見せる）
  w.prepare("INSERT INTO users VALUES (1, '利用者A')").run();
  w.prepare("INSERT INTO messages VALUES (?, 1, 'この件どうなりましたか')").run(BigInt(TRIG));
  w.prepare("INSERT INTO messages VALUES (?, 99, '先週の決定では〜です')").run(BigInt(MSG));
  // 文字列で渡すと SQLite が INTEGER として正確に保存する（JS の number を経由しない）
  w.prepare("INSERT INTO channels VALUES (?, 'general')").run(BigInt(CH));
  w.prepare(
    `INSERT INTO proactive_log (created_at, agent_id, kind, action, channel_id,
       trigger_message_id, posted_message_id, detail) VALUES ('2026-09-28T10:00',
       'agent1', 'rescue', 'spoke', ?, ?, ?, 'x')`,
  ).run(BigInt(CH), BigInt(TRIG), BigInt(MSG));
  w.close();
}

const { recentActivity } = await import("../server/db/queries.ts");
const { closeDb } = await import("../server/db/ro.ts");

afterAll(() => {
  closeDb();
  fs.rmSync(dir, { recursive: true, force: true });
});

describe("Discord ID を丸めずに返す", () => {
  it("タイムラインのチャンネルIDと投稿IDが桁落ちしない", () => {
    const rows = recentActivity();
    expect(rows).toHaveLength(1);
    expect(rows[0]?.channelId).toBe(CH);
    expect(rows[0]?.postedMessageId).toBe(MSG);
    expect(rows[0]?.channelName).toBe("general");
  });

  it("タイムラインに、きっかけの発言と投稿の冒頭を添える", () => {
    const [row] = recentActivity();
    expect(row?.triggerAuthor).toBe("利用者A");
    expect(row?.triggerExcerpt).toBe("この件どうなりましたか");
    expect(row?.postedExcerpt).toBe("先週の決定では〜です");
  });
});
