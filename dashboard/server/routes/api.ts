/**
 * REST API。すべて 127.0.0.1 からのみアクセスされる前提。
 * 書き込みは PATCH /api/config と POST /api/apply の2つだけに集約する。
 */
import { Hono } from "hono";

import { writeSnapshot, type ConfigOwner } from "../config/apply.ts";
import { parseScope, PatchError, toPatches } from "../config/patcher.ts";
import {
  ConfigError,
  patchConfig,
  patchMeetingConfig,
  readConfig,
} from "../config/store.ts";
import {
  capabilityRequests,
  counters,
  deployHistory,
  devJobs,
  glossary,
  recentActivity,
  roadmapItems,
  rules,
  subLoops,
  terms,
} from "../db/queries.ts";
import { avatarFor, letterAvatar } from "../ops/avatars.ts";
import { logInventory, ROTATE_THRESHOLD_BYTES, tail } from "../ops/logs.ts";
import { readReminders } from "../ops/state.ts";
import { allServiceStatus, restartService } from "../ops/status.ts";
import { SERVICE_BY_ID, type ServiceId } from "../paths.ts";
import {
  agentDetail,
  agentIndex,
  overview,
  pendingChanges,
  settingsView,
  stallAfterSec,
} from "../views.ts";

export const api = new Hono();

function fail(error: unknown): { message: string; issues?: { path: string; message: string }[] } {
  if (error instanceof ConfigError) return { message: error.message, issues: error.issues };
  if (error instanceof PatchError) return { message: error.message };
  console.error("[api] 予期しないエラー:", error);
  return { message: "サーバー側でエラーが発生しました（詳細はコンソール）" };
}

api.get("/overview", async (c) => c.json(await overview(await stallAfterSec())));

/** エージェントのアイコン（Discordの実アイコンをキャッシュして配信） */
api.get("/avatars/:id", async (c) => {
  const id = c.req.param("id");
  const png = await avatarFor(id);
  if (png !== null) {
    c.header("Content-Type", "image/png");
    c.header("Cache-Control", "public, max-age=3600");
    return c.body(new Uint8Array(png));
  }
  // アイコン未設定は普通の状態。404を返すとブラウザのコンソールが
  // 毎回赤くなり「壊れている」ように見えるので、頭文字の絵を返す
  c.header("Content-Type", "image/svg+xml; charset=utf-8");
  c.header("Cache-Control", "public, max-age=300");
  return c.body(letterAvatar(id));
});

api.get("/agents/:id", async (c) => {
  const detail = await agentDetail(c.req.param("id"));
  if (detail === null) return c.json({ message: "そんなエージェントは居ません" }, 404);
  return c.json(detail);
});

api.get("/settings", async (c) => c.json(await settingsView()));

api.get("/pending", async (c) => {
  const services = await allServiceStatus(await stallAfterSec());
  return c.json(await pendingChanges(services));
});

/** 設定の変更（保存のみ。BOTへの反映は /apply） */
api.patch("/config", async (c) => {
  try {
    const body = (await c.req.json()) as {
      scope?: string;
      changes?: { path: string; value: unknown }[];
    };
    const scope = parseScope(body.scope ?? "global");
    const changes = body.changes ?? [];
    if (changes.length === 0) return c.json({ message: "変更がありません" }, 400);

    if (scope.kind === "meeting") {
      const patches = toPatches(scope, changes, () => -1);
      const result = await patchMeetingConfig(patches);
      return c.json({ ok: true, backupPath: result.backupPath, applied: result.applied });
    }

    const config = await readConfig();
    const patches = toPatches(scope, changes, (id) => agentIndex(config, id));
    const result = await patchConfig(patches);
    return c.json({ ok: true, backupPath: result.backupPath, applied: result.applied });
  } catch (error) {
    const status = error instanceof ConfigError || error instanceof PatchError ? 400 : 500;
    return c.json(fail(error), status);
  }
});

/** 議事録BOTの話者名マッピングの追加・変更・削除 */
api.patch("/settings/meeting-users", async (c) => {
  try {
    const body = (await c.req.json()) as { mapping?: Record<string, string> };
    if (!body.mapping || typeof body.mapping !== "object") {
      return c.json({ message: "mapping が必要です" }, 400);
    }
    const result = await patchMeetingConfig([{ path: "user_mapping", value: body.mapping }]);
    return c.json({ ok: true, backupPath: result.backupPath });
  } catch (error) {
    return c.json(fail(error), error instanceof ConfigError ? 400 : 500);
  }
});

/** 未適用の変更をBOTへ反映する（＝該当プロセスを再起動する） */
api.post("/apply/:owner", async (c) => {
  const owner = c.req.param("owner") as ConfigOwner;
  if (!["archivebot", "devbot", "meetingbot"].includes(owner)) {
    return c.json({ message: "不明な対象です" }, 400);
  }
  try {
    const result = await restartService(owner);
    if (result.ok) {
      // 再起動したプロセスが読んだ内容を「適用済み」として焼き付ける
      await writeSnapshot(owner, await readConfig());
    }
    return c.json(result, result.ok ? 200 : 500);
  } catch (error) {
    return c.json(fail(error), 500);
  }
});

api.get("/ops/services", async (c) => c.json(await allServiceStatus(await stallAfterSec())));

api.post("/ops/services/:id/restart", async (c) => {
  const id = c.req.param("id") as ServiceId;
  if (!SERVICE_BY_ID.has(id)) return c.json({ message: "不明なサービスです" }, 400);
  try {
    const result = await restartService(id);
    return c.json(result, result.ok ? 200 : 500);
  } catch (error) {
    return c.json(fail(error), 500);
  }
});

api.get("/ops/logs", async (c) => {
  const inventory = await logInventory();
  return c.json({
    thresholdBytes: ROTATE_THRESHOLD_BYTES,
    items: inventory,
    note: "devbot のログは rotate-bot-logs.sh の対象外です（肥大しても自動退避されません）",
  });
});

api.get("/ops/logs/:id", async (c) => {
  try {
    const bytes = Number(c.req.query("bytes") ?? 64 * 1024);
    return c.json(await tail(c.req.param("id"), Math.min(Math.max(bytes, 1024), 1024 * 1024)));
  } catch (error) {
    return c.json(fail(error), 400);
  }
});

api.get("/ops/subloops", (c) => c.json(subLoops()));

api.get("/activity", (c) => {
  const limit = Number(c.req.query("limit") ?? 50);
  const includeSilent = c.req.query("silent") === "1";
  return c.json(recentActivity({ limit, includeSilent }));
});

api.get("/data/summary", async (c) => {
  const reminders = await readReminders();
  return c.json({
    counters: counters(),
    reminders: { active: reminders.active, error: reminders.error, total: reminders.total },
  });
});

api.get("/data/rules", (c) => c.json(rules()));
api.get("/data/capabilities", (c) => c.json(capabilityRequests()));
api.get("/data/roadmap", (c) => c.json(roadmapItems()));
api.get("/data/dev-jobs", (c) => c.json({ jobs: devJobs(), deploys: deployHistory() }));
api.get("/data/reminders", async (c) => c.json(await readReminders()));
// 名前まわりは2つで1組（正式表記を覚える辞書と、決め打ちで直す単語帳）なので
// 1タブ＝1リクエストにまとめる
api.get("/data/dictionary", (c) => c.json({ terms: terms(), glossary: glossary() }));
