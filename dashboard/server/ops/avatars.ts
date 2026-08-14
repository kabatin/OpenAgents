/**
 * エージェントのアイコン取得。
 *
 * 4体はDiscordの実Botなので、アイコンはDiscordのCDNにある。
 * 「画面とDiscordで同じ顔が出る」ことに意味があるので、自前で絵を作らず本物を取る。
 *
 * トークンはこのプロセスの中だけで使い、ブラウザには画像バイト列しか返さない。
 * 取得結果は cache/avatars/ に置き、24時間はそれを使う（起動のたびに叩かない）。
 *
 * 注意: DiscordのAPIは User-Agent が無いと 403 を返す（Cloudflare）。
 */
import fsp from "node:fs/promises";
import path from "node:path";

import { DASHBOARD_DIR, ARCHIVE_DIR } from "../paths.ts";
import { getPath, type Json } from "../config/objpath.ts";
import { readConfig } from "../config/store.ts";

const CACHE_DIR = path.join(DASHBOARD_DIR, "cache", "avatars");
const TTL_MS = 24 * 60 * 60 * 1000;
const USER_AGENT = "DiscordBot (local-dashboard, 1.0)";

type Meta = { hash: string | null; userId: string; fetchedAtMs: number };

async function readMeta(id: string): Promise<Meta | null> {
  try {
    return JSON.parse(await fsp.readFile(path.join(CACHE_DIR, `${id}.json`), "utf8")) as Meta;
  } catch {
    return null;
  }
}

/** そのエージェントのトークン（config.json 内。呼び出し側へは絶対に返さない）。 */
async function tokenFor(agentId: string): Promise<string | null> {
  const config = await readConfig();
  if (agentId === "devbot") {
    const t = getPath(config, "dev_bot.token");
    return typeof t === "string" && t.length > 0 ? t : null;
  }
  const agents = (config["agents"] ?? []) as Record<string, Json>[];
  const agent = agents.find((a) => a["id"] === agentId);
  const t = agent?.["token"];
  return typeof t === "string" && t.length > 0 ? t : null;
}

async function fetchFromDiscord(agentId: string): Promise<Buffer | null> {
  const token = await tokenFor(agentId);
  if (token === null) return null;

  const me = await fetch("https://discord.com/api/v10/users/@me", {
    headers: { Authorization: `Bot ${token}`, "User-Agent": USER_AGENT },
    signal: AbortSignal.timeout(10_000),
  });
  if (!me.ok) {
    console.error(`[avatar] ${agentId}: Discord API が ${me.status} を返しました`);
    return null;
  }
  const user = (await me.json()) as { id: string; avatar: string | null };
  if (user.avatar === null) return null;

  const ext = user.avatar.startsWith("a_") ? "gif" : "png";
  const cdn = `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.${ext}?size=128`;
  const img = await fetch(cdn, {
    headers: { "User-Agent": USER_AGENT },
    signal: AbortSignal.timeout(10_000),
  });
  if (!img.ok) {
    console.error(`[avatar] ${agentId}: CDN が ${img.status} を返しました`);
    return null;
  }
  const buf = Buffer.from(await img.arrayBuffer());
  await fsp.mkdir(CACHE_DIR, { recursive: true });
  await fsp.writeFile(path.join(CACHE_DIR, `${agentId}.png`), buf);
  await fsp.writeFile(
    path.join(CACHE_DIR, `${agentId}.json`),
    JSON.stringify({ hash: user.avatar, userId: user.id, fetchedAtMs: Date.now() } satisfies Meta),
  );
  return buf;
}

/** Webhook人格はローカルにアイコンファイルを持っている（採用時にCodexで生成したもの）。 */
async function localPersonaAvatar(agentId: string): Promise<Buffer | null> {
  const file = path.join(ARCHIVE_DIR, "avatars", `${agentId}.png`);
  try {
    return await fsp.readFile(file);
  } catch {
    return null;
  }
}

/**
 * アイコンのバイト列。無ければ null（画面はイニシャル表示にフォールバックする）。
 * キャッシュが新しければそれを返し、古ければ取り直す。取り直しに失敗したら
 * 古いキャッシュをそのまま使う（ネットワークが死んでもアイコンが消えない）。
 */
export async function avatarFor(agentId: string): Promise<Buffer | null> {
  if (!/^[a-z0-9_-]{1,32}$/i.test(agentId)) return null;

  const cached = path.join(CACHE_DIR, `${agentId}.png`);
  const meta = await readMeta(agentId);
  const fresh = meta !== null && Date.now() - meta.fetchedAtMs < TTL_MS;

  if (fresh) {
    try {
      return await fsp.readFile(cached);
    } catch {
      // キャッシュファイルだけ消えた場合は取り直す
    }
  }

  try {
    const fetched = await fetchFromDiscord(agentId);
    if (fetched !== null) return fetched;
  } catch (error) {
    console.error(`[avatar] ${agentId}: 取得に失敗:`, error);
  }

  // Discordから取れない（Webhook人格・オフライン・トークン無し）
  try {
    return await fsp.readFile(cached);
  } catch {
    return localPersonaAvatar(agentId);
  }
}

/** 名前から安定した色を選ぶ（同じIDなら毎回同じ色）。 */
function hueFor(id: string): number {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.codePointAt(0)!) % 360;
  return h;
}

/**
 * アイコン未設定のときに返す、頭文字だけの絵。
 *
 * 404 を返すとブラウザのコンソールが毎回赤くなり「壊れている」ように見える。
 * アイコンが無いのはごく普通の状態なので、異常として扱わない。
 */
export function letterAvatar(id: string): string {
  const letter = [...(id || "?")][0]!.toUpperCase();
  const hue = hueFor(id);
  const safe = letter.replace(/[<>&"']/g, "");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect width="64" height="64" rx="32" fill="hsl(${hue} 45% 88%)"/>
  <text x="32" y="41" text-anchor="middle" font-size="28"
        font-family="system-ui, sans-serif" fill="hsl(${hue} 40% 32%)">${safe}</text>
</svg>`;
}
