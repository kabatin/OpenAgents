/**
 * 初期セットアップのAPI。
 *
 * 画面から「トークンを確かめる」「チャンネルを選ぶ」「AIを試す」「挨拶を投稿する」
 * を行えるようにする。設定ファイルを手で書かせないための入口。
 *
 * ## 守っていること
 *
 * - **状態を変える操作はすべて POST**（`server/security.ts` のCSRF検査を通す）。
 *   トークン検証も、他所のページから叩かれないよう POST にしてある
 * - **トークンをレスポンスに載せない**。検証の結果として返すのは
 *   Bot名とアイコンだけ
 * - ファイルの書き込み先は personas/ と knowledge/ に限る（personas.ts）
 */
import { Hono } from "hono";

import { CONFIG_PATH } from "../paths.ts";
import {
  addAgent,
  configExists,
  ConfigError,
  createInitialConfig,
  patchConfig,
  readConfig,
  removeAgent,
  togglePersonaFile,
} from "../config/store.ts";
import { getPath } from "../config/objpath.ts";
import { restartService } from "../ops/status.ts";
import {
  checkIntents,
  DiscordError,
  inviteUrl,
  listChannels,
  listGuilds,
  postMessage,
  verifyToken,
} from "../setup/discord.ts";
import { detectProviders, testProvider } from "../setup/llm.ts";
import * as personas from "../setup/personas.ts";

export const setup = new Hono();

/** 例外を「人間に見せる1文」へ畳む。 */
function fail(e: unknown): { message: string; hint?: string } {
  if (e instanceof DiscordError) return { message: e.message, hint: e.hint };
  if (e instanceof personas.PersonaError) return { message: e.message };
  if (e instanceof ConfigError) return { message: e.message };
  return { message: e instanceof Error ? e.message : String(e) };
}

async function body(c: { req: { json: () => Promise<unknown> } }): Promise<Record<string, string>> {
  try {
    return ((await c.req.json()) ?? {}) as Record<string, string>;
  } catch {
    return {};
  }
}

// --- 状態 -----------------------------------------------------------------

/** セットアップが必要かどうか。画面はまずこれを見る。 */
setup.get("/state", async (c) => {
  const exists = configExists();
  if (!exists) {
    return c.json({ configExists: false, needsSetup: true, agentCount: 0, hasGuild: false });
  }
  const config = await readConfig();
  const agents = (getPath(config, "agents") ?? []) as unknown[];
  const guild = String(getPath(config, "guild_id") ?? "");
  return c.json({
    configExists: true,
    needsSetup: agents.length === 0 || guild === "",
    agentCount: agents.length,
    hasGuild: guild !== "",
    configPath: CONFIG_PATH,
  });
});

/** 設定ファイルをまだ作っていなければ作る。 */
setup.post("/init", async (c) => {
  try {
    return c.json(await createInitialConfig());
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

// --- Discord ---------------------------------------------------------------

/**
 * トークンが有効か確かめる。
 * 返すのは Bot名・アイコン・招待URL・intentの状態だけ（トークンは返さない）。
 */
setup.post("/platform/verify", async (c) => {
  const { token } = await body(c);
  try {
    const identity = await verifyToken(token ?? "");
    const intent = await checkIntents(token ?? "");
    return c.json({
      ok: true,
      bot: identity,
      inviteUrl: inviteUrl(identity.id),
      intents: intent,
    });
  } catch (e) {
    return c.json({ ok: false, ...fail(e) }, 400);
  }
});

/** Botが参加しているサーバーの一覧。 */
setup.post("/platform/guilds", async (c) => {
  const { token } = await body(c);
  try {
    return c.json({ guilds: await listGuilds(token ?? "") });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/**
 * サーバー内のチャンネル一覧（IDを手で調べさせないため）。
 * guildId 未指定なら、設定済みのサーバーを使う
 * （2体目以降の追加では、もうサーバーは決まっている）。
 */
setup.post("/platform/channels", async (c) => {
  const { token, guildId } = await body(c);
  try {
    let target = guildId ?? "";
    if (target === "") {
      const config = await readConfig();
      target = String(getPath(config, "guild_id") ?? "");
    }
    return c.json({ channels: await listChannels(token ?? "", target) });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

// --- LLM -------------------------------------------------------------------

/** インストール済みのAIツールを調べる。 */
setup.post("/llm/detect", async (c) => {
  try {
    return c.json(await detectProviders());
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/** 実際に一言返させる（設定できた気がする、で終わらせない）。 */
setup.post("/llm/test", async (c) => {
  const { provider, model } = await body(c);
  const result = await testProvider(provider ?? "claude", model ?? "");
  return c.json(result, result.ok ? 200 : 400);
});

// --- 性格 -------------------------------------------------------------------

/** relPath → 使っているエージェント。画面の「使用中」表示と削除ガードに使う。 */
async function usageMap(): Promise<Record<string, { id: string; name: string }[]>> {
  const config = await readConfig();
  const agents = (getPath(config, "agents") ?? []) as {
    id?: string;
    name?: string;
    persona_files?: string[];
  }[];
  const map: Record<string, { id: string; name: string }[]> = {};
  for (const a of agents) {
    for (const rel of a.persona_files ?? []) {
      // config には `../personas/x.md` のような相対も書けるので末尾2つで見る
      const key = rel.split("/").slice(-2).join("/");
      (map[key] ??= []).push({ id: String(a.id ?? ""), name: String(a.name ?? a.id ?? "") });
    }
  }
  return map;
}

function withUsage(
  files: personas.PersonaFile[],
  usage: Record<string, { id: string; name: string }[]>,
) {
  return files.map((f) => ({ ...f, usedBy: usage[f.relPath] ?? [] }));
}

setup.get("/personas", async (c) => {
  const usage = await usageMap();
  return c.json({
    personas: withUsage(personas.list("personas"), usage),
    knowledge: withUsage(personas.list("knowledge"), usage),
    agents: ((getPath(await readConfig(), "agents") ?? []) as { id?: string; name?: string }[]).map(
      (a) => ({ id: String(a.id ?? ""), name: String(a.name ?? a.id ?? "") }),
    ),
  });
});

/** 見本をコピーして自分用のファイルを作る（名前はサーバーが決める）。 */
setup.post("/personas/copy", async (c) => {
  const { area, fileName } = await body(c);
  try {
    const a = (area ?? "personas") as personas.AreaName;
    const newName = personas.copyName(a, fileName ?? "");
    await personas.write(a, newName, personas.read(a, fileName ?? ""));
    return c.json({ ok: true, fileName: newName, relPath: `${a}/${newName}` });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/** このファイルをエージェントに読ませる / 読ませないを切り替える。 */
setup.post("/personas/use", async (c) => {
  const raw = (await c.req.json().catch(() => ({}))) as {
    agentId?: string;
    relPath?: string;
    use?: boolean;
  };
  try {
    const result = await togglePersonaFile(
      raw.agentId ?? "",
      raw.relPath ?? "",
      raw.use === true,
    );
    return c.json({ ok: true, backupPath: result.backupPath });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

setup.post("/personas/delete", async (c) => {
  const { area, fileName } = await body(c);
  try {
    const a = (area ?? "personas") as personas.AreaName;
    const usage = await usageMap();
    const users = usage[`${a}/${fileName}`] ?? [];
    if (users.length > 0) {
      // 使用中のまま消すと、次の起動で読めないファイルを指したままになる
      return c.json(
        {
          message:
            `${users.map((u) => u.name).join("・")} が使っています。` +
            "先に「使う」をオフにしてください",
        },
        400,
      );
    }
    await personas.remove(a, fileName ?? "");
    return c.json({ ok: true });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

setup.get("/personas/:area/:file", (c) => {
  try {
    const area = c.req.param("area") as personas.AreaName;
    return c.json({ content: personas.read(area, c.req.param("file")) });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

setup.post("/personas/save", async (c) => {
  const { area, fileName, content } = await body(c);
  try {
    await personas.write((area ?? "personas") as personas.AreaName, fileName ?? "", content ?? "");
    return c.json({ ok: true, relPath: `${area}/${fileName}` });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/** テンプレートから自分用の性格ファイルを作る。 */
setup.post("/personas/create", async (c) => {
  const raw = (await c.req.json().catch(() => ({}))) as {
    area?: string;
    template?: string;
    fileName?: string;
    values?: Record<string, string>;
  };
  try {
    const created = await personas.createFromTemplate(
      (raw.area ?? "personas") as personas.AreaName,
      raw.template ?? "",
      raw.fileName ?? "",
      raw.values ?? {},
    );
    return c.json({ ok: true, file: created });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

// --- エージェント -------------------------------------------------------------

setup.post("/agents/add", async (c) => {
  const raw = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
  try {
    const result = await addAgent({
      id: String(raw["id"] ?? ""),
      name: String(raw["name"] ?? ""),
      token: String(raw["token"] ?? ""),
      home_channel_id: String(raw["home_channel_id"] ?? ""),
      persona_files: (raw["persona_files"] as string[]) ?? [],
      role: raw["role"] === undefined ? "" : String(raw["role"]),
    });
    return c.json({ ok: true, backupPath: result.backupPath });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

setup.post("/agents/remove", async (c) => {
  const { id } = await body(c);
  try {
    const result = await removeAgent(id ?? "");
    return c.json({ ok: true, backupPath: result.backupPath });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

// --- 仕上げ -------------------------------------------------------------------

/** サーバーと管理者を設定に保存する。 */
setup.post("/platform/save", async (c) => {
  const { guildId, adminId } = await body(c);
  try {
    const patches = [{ path: "guild_id", value: guildId ?? "" }];
    if (adminId) patches.push({ path: "admins", value: [adminId] as never });
    await patchConfig(patches);
    return c.json({ ok: true });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/** 使うAIを保存する。 */
setup.post("/llm/save", async (c) => {
  const { provider, model } = await body(c);
  try {
    const patches = [{ path: "llm.provider", value: provider ?? "claude" }];
    if (model !== undefined) patches.push({ path: "llm.model", value: model });
    await patchConfig(patches);
    return c.json({ ok: true });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/**
 * 設定したチャンネルへ挨拶を投稿する。
 * ここまで来て初めて「本当に動いた」と分かる。
 */
setup.post("/hello", async (c) => {
  const { agentId } = await body(c);
  try {
    const config = await readConfig();
    const agents = (getPath(config, "agents") ?? []) as Record<string, unknown>[];
    const agent = agentId ? agents.find((a) => a["id"] === agentId) : agents[0];
    if (!agent) return c.json({ message: "エージェントが登録されていません" }, 400);
    const token = String(agent["token"] ?? "");
    const channelId = String(agent["home_channel_id"] ?? "");
    if (!token || !channelId) {
      return c.json({ message: "トークンかチャンネルが未設定です" }, 400);
    }
    const name = String(agent["name"] ?? "エージェント");
    const posted = await postMessage(
      token,
      channelId,
      `はじめまして、${name}です。ここに常駐します。よろしくお願いします。`,
    );
    const guildId = String(getPath(config, "guild_id") ?? "");
    return c.json({
      ok: true,
      messageId: posted.id,
      jumpUrl: `https://discord.com/channels/${guildId}/${channelId}/${posted.id}`,
    });
  } catch (e) {
    return c.json(fail(e), 400);
  }
});

/** 設定を反映するためにBOTを起動／再起動する。 */
setup.post("/launch", async (c) => {
  const result = await restartService("archivebot");
  return c.json(result, result.ok ? 200 : 400);
});
