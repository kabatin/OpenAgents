/**
 * ダッシュボードのサーバー本体。
 *
 * セキュリティ方針（このプロセスは Botトークンを読めるので厳しめに）:
 *  - Host を検査して DNSリバインディングを塞ぐ（許すのは自機・LAN・.local・*.ts.net）
 *  - 状態を変える操作は別オリジンから叩けないようにする（security.ts / CSRF検査）
 *  - 秘密情報はマスクしてからでないと応答に載せない（views.ts の責務）
 *  - archive.db は readonly で開く（db/ro.ts）
 *  - パスワードは既定で無し。DASHBOARD_PASSWORD を入れると Basic認証が有効になる
 *
 * 環境変数:
 *  DASHBOARD_HOST          待ち受けアドレス（既定 127.0.0.1 = 自分のPCだけ）
 *  DASHBOARD_PORT          ポート（既定 8787）
 *  DASHBOARD_ALLOWED_HOSTS 追加で許可するHost（カンマ区切り。Tailscale等）
 *  DASHBOARD_PASSWORD      設定するとBasic認証が有効（ユーザー名は admin 固定）
 *
 * 環境変数が無ければ config.json の dashboard セクションを見る。
 * **既定は 127.0.0.1**（自分のPCからのみ）。この画面はBotトークンを読めるので、
 * LANに出すのは利用者が明示的に選んだときだけにする。
 */
import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { Hono } from "hono";
import { basicAuth } from "hono/basic-auth";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { api } from "./routes/api.ts";
import { events } from "./routes/events.ts";
import { setup } from "./routes/setup.ts";
import { closeDb } from "./db/ro.ts";
import { CONFIG_PATH, WEB_DIST_DIR } from "./paths.ts";
import { checkCsrf, extraAllowedHosts, isAllowedHost } from "./security.ts";

/** 設定ファイルの dashboard セクション（読めなければ空）。 */
function dashboardConfig(): Record<string, unknown> {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, "utf8");
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return (parsed["dashboard"] ?? {}) as Record<string, unknown>;
  } catch {
    return {};
  }
}

const DASH = dashboardConfig();
const PORT = Number(process.env["DASHBOARD_PORT"] ?? DASH["port"] ?? 8787);
const HOST = process.env["DASHBOARD_HOST"] ?? (DASH["host"] as string) ?? "127.0.0.1";
const EXTRA_HOSTS = extraAllowedHosts(process.env["DASHBOARD_ALLOWED_HOSTS"]);
const PASSWORD = process.env["DASHBOARD_PASSWORD"] || (DASH["password"] as string) || undefined;

const app = new Hono();

// 1) Host検査: 公開ドメイン名で来たら拒否（DNSリバインディング対策）
app.use("*", async (c, next) => {
  const host = c.req.header("host");
  if (!isAllowedHost(host, EXTRA_HOSTS)) {
    return c.json(
      {
        message:
          `このホスト名でのアクセスは許可されていません: ${host ?? "(なし)"}。` +
          "自機・同一LAN・.local・*.ts.net のみ許可しています" +
          "（追加は DASHBOARD_ALLOWED_HOSTS）",
      },
      403,
    );
  }
  c.header("X-Content-Type-Options", "nosniff");
  c.header("Referrer-Policy", "no-referrer");
  await next();
});

// 2) CSRF検査: 別オリジンからの状態変更を拒否
//    （悪意あるページが再起動POSTを投げてくるのを止める。パスワードでは防げない部分）
app.use("*", async (c, next) => {
  const verdict = checkCsrf({
    method: c.req.method,
    host: c.req.header("host"),
    origin: c.req.header("origin"),
    secFetchSite: c.req.header("sec-fetch-site"),
  });
  if (!verdict.ok) return c.json({ message: verdict.reason }, 403);
  await next();
});

// 3) パスワード（任意）
if (PASSWORD !== undefined && PASSWORD !== "") {
  app.use("*", basicAuth({ username: "admin", password: PASSWORD }));
}

app.route("/api", api);
app.route("/api", events);
app.route("/api/setup", setup);

app.get("/api/health", (c) =>
  c.json({ ok: true, configPath: CONFIG_PATH, pid: process.pid, auth: PASSWORD ? "basic" : "none" }),
);

// 本番（npm run build 済み）は dist/ を配信。開発中は Vite 側が担当する。
if (fs.existsSync(WEB_DIST_DIR)) {
  app.use(
    "/*",
    serveStatic({
      root: path.relative(process.cwd(), WEB_DIST_DIR) || ".",
    }),
  );
  app.get("*", (c) => {
    const html = fs.readFileSync(path.join(WEB_DIST_DIR, "index.html"), "utf8");
    return c.html(html);
  });
}

/** 起動ログに実際に開けるURLを出す（LANのIPを調べ直さなくていいように）。 */
function reachableUrls(port: number): string[] {
  const urls = [`http://127.0.0.1:${port}`];
  if (HOST === "0.0.0.0" || HOST === "::") {
    for (const addrs of Object.values(os.networkInterfaces())) {
      for (const a of addrs ?? []) {
        if (a.family === "IPv4" && !a.internal) urls.push(`http://${a.address}:${port}`);
      }
    }
    urls.push(`http://${os.hostname()}:${port}`);
  }
  return urls;
}

if (HOST !== "127.0.0.1" && HOST !== "localhost" && !PASSWORD) {
  // この画面はBotトークンを読め、設定変更もDiscordへの投稿もBOTの再起動もできる。
  // パスワード無しでLANに出すのは、同じネットワークの誰にでもそれを許すこと。
  // 警告だけで起動を続けると、警告は必ず見落とされる
  console.error(
    "\n❌ 起動を中止しました。\n\n" +
      `  この画面を ${HOST} で公開しようとしていますが、パスワードが未設定です。\n` +
      "  この画面からは Botトークンの保存・設定変更・Discordへの投稿・BOTの再起動が\n" +
      "  できるため、パスワード無しでLANに出すことはできません。\n\n" +
      "  config.json の dashboard.password を設定してから、もう一度起動してください。\n" +
      "  （自分のPCからだけ使うなら dashboard.host を 127.0.0.1 に戻してください）\n",
  );
  process.exit(1);
}

const server = serve({ fetch: app.fetch, hostname: HOST, port: PORT }, (info) => {
  console.log("AIエージェント管理ダッシュボード");
  for (const url of reachableUrls(info.port)) console.log(`  ${url}`);
  console.log(
    PASSWORD ? "  認証: Basic（ユーザー名 admin）" : "  認証: なし（CSRF検査とHost検査のみ）",
  );
  if (!fs.existsSync(WEB_DIST_DIR)) {
    console.log("  ※ 画面は Vite 開発サーバー http://127.0.0.1:5173 で見てください");
  }
});

const shutdown = () => {
  server.close();
  closeDb();
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
