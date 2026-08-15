/**
 * セットアップ中に Discord へ問い合わせる部分。
 *
 * 目的は**利用者にIDをコピーさせないこと**。開発者モードを有効にして
 * 右クリックでIDをコピーして貼り付ける、という手順がいちばんの脱落ポイント
 * なので、トークンさえ通ればサーバーもチャンネルも一覧から選べるようにする。
 *
 * ここが返すのは「事実」だけ。設定への保存は routes 側が行う。
 */

const API = "https://discord.com/api/v10";
const TIMEOUT_MS = 10_000;

export type BotIdentity = {
  id: string;
  username: string;
  /** 表示用のアイコンURL（無ければ空） */
  avatarUrl: string;
};

export type GuildSummary = { id: string; name: string; iconUrl: string };

export type ChannelSummary = {
  id: string;
  name: string;
  /** "text" | "voice" | "category" | "other" */
  kind: string;
  parentId: string | null;
};

export class DiscordError extends Error {
  constructor(
    message: string,
    readonly hint = "",
  ) {
    super(message);
    this.name = "DiscordError";
  }
}

async function call(token: string, path: string): Promise<unknown> {
  const trimmed = token.trim();
  if (!trimmed) throw new DiscordError("トークンが空です");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      headers: { Authorization: `Bot ${trimmed}` },
      signal: controller.signal,
    });
  } catch (e) {
    const cause = e instanceof Error ? e.message : String(e);
    throw new DiscordError(`Discordに接続できませんでした: ${cause}`, "ネットワークを確認してください");
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) {
    throw new DiscordError(
      "トークンが違います",
      "Developer Portal の Bot ページで「Reset Token」を押し、表示された文字列を丸ごと貼り付けてください（Client Secret ではありません）",
    );
  }
  if (res.status === 403) {
    throw new DiscordError("権限が足りません", "Botをサーバーに招待し直してください");
  }
  if (res.status === 429) {
    throw new DiscordError("Discord側で回数制限に掛かりました", "少し待ってからもう一度お試しください");
  }
  if (!res.ok) {
    throw new DiscordError(`Discordが${res.status}を返しました`);
  }
  return res.json();
}

/** トークンが有効か確かめ、Bot自身の情報を返す。 */
export async function verifyToken(token: string): Promise<BotIdentity> {
  const me = (await call(token, "/users/@me")) as {
    id: string;
    username: string;
    avatar: string | null;
  };
  return {
    id: me.id,
    username: me.username,
    avatarUrl: me.avatar
      ? `https://cdn.discordapp.com/avatars/${me.id}/${me.avatar}.png?size=128`
      : "",
  };
}

/** Botが参加しているサーバーの一覧。 */
export async function listGuilds(token: string): Promise<GuildSummary[]> {
  const guilds = (await call(token, "/users/@me/guilds")) as {
    id: string;
    name: string;
    icon: string | null;
  }[];
  return guilds.map((g) => ({
    id: g.id,
    name: g.name,
    iconUrl: g.icon ? `https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=64` : "",
  }));
}

/** Discord のチャンネル種別（数値）を、人間に分かる名前へ。 */
function channelKind(type: number): string {
  if (type === 0 || type === 5) return "text";
  if (type === 2 || type === 13) return "voice";
  if (type === 4) return "category";
  return "other";
}

/** サーバー内のチャンネル一覧（カテゴリも含む。並びは Discord の表示順）。 */
export async function listChannels(token: string, guildId: string): Promise<ChannelSummary[]> {
  if (!guildId.trim()) throw new DiscordError("サーバーが選ばれていません");
  const channels = (await call(token, `/guilds/${guildId}/channels`)) as {
    id: string;
    name: string;
    type: number;
    parent_id: string | null;
    position: number;
  }[];
  return channels
    .slice()
    .sort((a, b) => a.position - b.position)
    .map((c) => ({
      id: c.id,
      name: c.name,
      kind: channelKind(c.type),
      parentId: c.parent_id,
    }));
}

/**
 * チャンネルに投稿する（セットアップ最後の「動きました」用）。
 * 失敗の理由をそのまま見せたいので、握りつぶさない。
 */
export async function postMessage(
  token: string,
  channelId: string,
  content: string,
): Promise<{ id: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${API}/channels/${channelId}/messages`, {
      method: "POST",
      headers: {
        Authorization: `Bot ${token.trim()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ content }),
      signal: controller.signal,
    });
    if (res.status === 403) {
      throw new DiscordError(
        "そのチャンネルへ投稿する権限がありません",
        "Botロールに「メッセージを送信」を許可するか、別のチャンネルを選んでください",
      );
    }
    if (!res.ok) throw new DiscordError(`投稿できませんでした（${res.status}）`);
    return (await res.json()) as { id: string };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * アプリ情報の flags から、必要な2つの Privileged Intent の状態を読む（純粋関数）。
 *
 * どちらも agent_runtime.py が要求している。片方でも欠けると動かないのに
 * 症状が違うので、まとめて1つの真偽値にせず別々に返す。
 *   - MESSAGE CONTENT: 接続はできるが発言の中身が空で届き、何をしても無反応
 *   - SERVER MEMBERS : そもそも接続時に弾かれる（PrivilegedIntentsRequired）
 *
 * `..._LIMITED` は「100サーバー未満なら使える」状態で、この用途では有効と同じ。
 */
export function intentsFromFlags(flags: number): {
  messageContent: boolean;
  serverMembers: boolean;
} {
  return {
    // 1 << 18: GATEWAY_MESSAGE_CONTENT / 1 << 19: ..._LIMITED
    messageContent: (flags & (1 << 18)) !== 0 || (flags & (1 << 19)) !== 0,
    // 1 << 16: GATEWAY_GUILD_MEMBERS / 1 << 17: ..._LIMITED
    serverMembers: (flags & (1 << 16)) !== 0 || (flags & (1 << 17)) !== 0,
  };
}

/** 足りていない intent を、直し方つきの1文にする（純粋関数）。 */
export function intentWarning(state: {
  messageContent: boolean;
  serverMembers: boolean;
}): string {
  const missing: string[] = [];
  if (!state.messageContent) missing.push("MESSAGE CONTENT INTENT");
  if (!state.serverMembers) missing.push("SERVER MEMBERS INTENT");
  if (missing.length === 0) return "";
  const why = !state.serverMembers
    ? "これが無いとBOTは接続そのものができません"
    : "これが無いと、接続はできても発言の中身が届かず無反応になります";
  return (
    `${missing.join(" と ")} が無効です。Developer Portal の Bot ページ` +
    `（Privileged Gateway Intents）で有効にして Save Changes を押してください` +
    `（${why}）`
  );
}

/**
 * Botに必要な Privileged Intent が有効になっているか。
 *
 * 最大の脱落ポイントなのでセットアップ中に検出して警告する。
 * intent の状態はAPIから直接は読めないため、アプリ情報の flags を見る。
 */
export async function checkIntents(token: string): Promise<{
  enabled: boolean | null;
  messageContent: boolean | null;
  serverMembers: boolean | null;
  detail: string;
}> {
  try {
    const app = (await call(token, "/applications/@me")) as { flags?: number };
    const state = intentsFromFlags(app.flags ?? 0);
    const warning = intentWarning(state);
    return {
      enabled: warning === "",
      messageContent: state.messageContent,
      serverMembers: state.serverMembers,
      detail: warning === "" ? "必要な権限は有効です" : warning,
    };
  } catch {
    // 取れないことと無効であることは違う。分からないなら分からないと言う
    return {
      enabled: null,
      messageContent: null,
      serverMembers: null,
      detail: "設定を確認できませんでした",
    };
  }
}

/** Botをサーバーへ招待するURL（必要な権限だけを要求する）。 */
export function inviteUrl(applicationId: string): string {
  // 読む・書く・履歴を読む・スレッドを作る・リアクションを付ける・ファイル添付
  const permissions = [
    1n << 10n, // View Channels
    1n << 11n, // Send Messages
    1n << 16n, // Read Message History
    1n << 15n, // Embed Links
    1n << 14n, // Attach Files
    1n << 6n, // Add Reactions
    1n << 34n, // Create Public Threads
    1n << 38n, // Send Messages in Threads
  ].reduce((a, b) => a | b, 0n);
  const params = new URLSearchParams({
    client_id: applicationId,
    scope: "bot",
    permissions: permissions.toString(),
  });
  return `https://discord.com/api/oauth2/authorize?${params.toString()}`;
}
