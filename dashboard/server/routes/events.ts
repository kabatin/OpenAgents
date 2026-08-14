/**
 * SSE。1本の接続に複数の名前付きイベントを多重化する。
 *
 * ポーリング間隔は「そのデータが実際に変わりうる速さ」に合わせる。
 * launchctl と lsof は外部プロセス起動なので、無闇に短くしない。
 */
import { Hono } from "hono";
import { streamSSE } from "hono/streaming";

import { maxActivityId, recentActivity, todayQuota } from "../db/queries.ts";
import { tail, watchLog } from "../ops/logs.ts";
import { allServiceStatus } from "../ops/status.ts";
import { agentsOf, pendingChanges, stallAfterSec } from "../views.ts";
import { readConfig } from "../config/store.ts";

export const events = new Hono();

const STATUS_MS = 5000;
const QUOTA_MS = 30_000;
const ACTIVITY_MS = 10_000;

events.get("/events", (c) =>
  streamSSE(c, async (stream) => {
    let closed = false;
    stream.onAbort(() => {
      closed = true;
    });

    let cursor = maxActivityId();
    let lastQuota = 0;
    let lastStatus = 0;
    let lastActivity = Date.now();
    let lastPendingJson = "";

    const send = async (event: string, data: unknown) => {
      await stream.writeSSE({ event, data: JSON.stringify(data) });
    };

    // 接続直後に現在値を1回流す（画面が空のまま待たされないように）
    const stall = await stallAfterSec();
    const services = await allServiceStatus(stall);
    await send("status", services);
    await send("pending", await pendingChanges(services));
    const config = await readConfig();
    await send(
      "quota",
      todayQuota(agentsOf(config).map((a) => String(a["id"]))),
    );

    while (!closed) {
      const now = Date.now();
      try {
        if (now - lastStatus >= STATUS_MS) {
          lastStatus = now;
          const svc = await allServiceStatus(stall);
          await send("status", svc);

          // 未適用差分はサービス起動時刻に依存するので status と同じ周期で見る
          const pending = await pendingChanges(svc);
          const json = JSON.stringify(pending);
          if (json !== lastPendingJson) {
            lastPendingJson = json;
            await send("pending", pending);
          }
        }
        if (now - lastQuota >= QUOTA_MS) {
          lastQuota = now;
          const cfg = await readConfig();
          await send(
            "quota",
            todayQuota(agentsOf(cfg).map((a) => String(a["id"]))),
          );
        }
        if (now - lastActivity >= ACTIVITY_MS) {
          lastActivity = now;
          const fresh = recentActivity({ sinceId: cursor, limit: 50 });
          if (fresh.length > 0) {
            cursor = Math.max(...fresh.map((r) => r.id));
            await send("activity", fresh);
          }
        }
      } catch (error) {
        console.error("[sse] 配信に失敗:", error);
      }
      await stream.sleep(1000);
    }
  }),
);

/** ログの追従（画面でログを開いている間だけ接続する） */
events.get("/logs/:id/stream", (c) => {
  const id = c.req.param("id");
  return streamSSE(c, async (stream) => {
    let closed = false;
    stream.onAbort(() => {
      closed = true;
    });

    const initial = await tail(id, 32 * 1024);
    await stream.writeSSE({ event: "init", data: JSON.stringify(initial) });

    const queue: unknown[] = [];
    const watcher = watchLog(id, initial.offset, (lines) => queue.push(...lines));

    try {
      while (!closed) {
        if (queue.length > 0) {
          const batch = queue.splice(0, queue.length);
          await stream.writeSSE({ event: "lines", data: JSON.stringify(batch) });
        }
        await stream.sleep(700);
      }
    } finally {
      watcher.close();
    }
  });
});
