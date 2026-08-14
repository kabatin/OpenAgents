/**
 * config.json の検証。
 *
 * 目的は「型の取り違え」と「BOTが起動しなくなる状態」を書き込む前に止めること。
 * 未知のキー（`_comment` など）は passthrough で必ず残す — このファイルは
 * 人間が手で育ててきた設定であり、こちらが知らない鍵を消してはいけない。
 *
 * 起動時の不変条件は chatbot/agent_runtime.py の validate_config() と
 * devbot/bot.py load_dev_config に対応する。
 */
import { z } from "zod";

const idLike = z.union([z.string(), z.number()]);

const agentSchema = z
  .object({
    id: z.string().min(1, "エージェントIDは必須です"),
    name: z.string().min(1, "表示名は必須です"),
    token: z.string(),
    home_channel_id: idLike,
    persona_files: z.array(z.string()),
    role: z.string().optional(),
    archiver: z.boolean().optional(),
    require_mention: z.boolean().optional(),
    runner_enabled: z.boolean().optional(),
    peer_review: z.string().nullable().optional(),
    skills: z.record(z.unknown()).optional(),
    proactive: z.record(z.unknown()).optional(),
    self_review: z.record(z.unknown()).optional(),
    session_resume: z.record(z.unknown()).optional(),
    thread_reply: z.record(z.unknown()).optional(),
  })
  .passthrough();

export const configSchema = z
  .object({
    guild_id: idLike,
    agents: z.array(agentSchema),
    history_limit: z.number().int().positive().optional(),
    context_history_limit: z.number().int().positive().optional(),
    max_bot_chain: z.number().int().positive().optional(),
    max_concurrent_answers: z.number().int().positive().optional(),
    admins: z.array(idLike).optional(),
    agent_category_id: idLike.nullable().optional(),
    llm: z.record(z.unknown()).optional(),
    integrations: z.record(z.unknown()).optional(),
    dashboard: z.record(z.unknown()).optional(),
    supervisor: z.record(z.unknown()).optional(),
    dev_bot: z.record(z.unknown()).optional(),
    meeting_bot: z.record(z.unknown()).optional(),
  })
  .passthrough();

export type AppConfig = z.infer<typeof configSchema>;

export const meetingConfigSchema = z
  .object({
    enabled: z.boolean().optional(),
    token: z.string().optional(),
    guild_id: idLike.optional(),
    voice_channel_name: z.string().optional(),
    user_mapping: z.record(z.string()).optional(),
  })
  .passthrough();

export type MeetingConfig = z.infer<typeof meetingConfigSchema>;

export type ValidationIssue = { path: string; message: string };

/**
 * **設定を壊す**変更を検査する（書き込む前の最後の砦）。
 *
 * ここが見るのは「壊れているか」であって「完成しているか」ではない。
 * セットアップの途中は、エージェントがまだ0体でもサーバー未設定でも正常で、
 * そこで書き込みを拒むと**設定を作る操作そのものができなくなる**
 * （実際に、サーバーを保存する最初の一手が弾かれていた）。
 *
 * 「起動してよいか」の判断は Python 側の `core/config.py: validate()` が持つ。
 */
export function checkInvariants(cfg: unknown): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const parsed = configSchema.safeParse(cfg);
  if (!parsed.success) {
    for (const e of parsed.error.errors) {
      issues.push({ path: e.path.join("."), message: e.message });
    }
    return issues;
  }
  const c = parsed.data;

  // エージェントが0体なのは「セットアップ前」であって、壊れてはいない
  if (c.agents.length > 0) {
    const archivers = c.agents.filter((a) => a.archiver === true);
    if (archivers.length !== 1) {
      issues.push({
        path: "agents[].archiver",
        message: `会話を記録する担当はちょうど1体でなければBOTが起動しません（現在 ${archivers.length} 体）`,
      });
    }
    const archiver = archivers[0];
    if (archiver && archiver.token.trim() === "") {
      issues.push({
        path: `agents.${archiver.id}.token`,
        message: "会話を記録する担当のトークンが空です（BOTが起動しません）",
      });
    }
  }

  const ids = c.agents.map((a) => a.id);
  const dupes = ids.filter((id, i) => ids.indexOf(id) !== i);
  if (dupes.length > 0) {
    issues.push({
      path: "agents[].id",
      message: `エージェントIDが重複しています: ${[...new Set(dupes)].join(", ")}`,
    });
  }

  // 開発BOTは既定オフ。**有効にしたときだけ**トークンを要求する。
  // セクションの存在だけで必須にすると、雛形（enabled:false, token:""）の
  // ままではエージェントを1体も追加できなくなる（Python側の validate と揃える）
  const devBot = c.dev_bot as Record<string, unknown> | undefined;
  const devToken = devBot?.["token"];
  if (devBot?.["enabled"] === true && (typeof devToken !== "string" || devToken.trim() === "")) {
    issues.push({
      path: "dev_bot.token",
      message: "開発BOTのトークンが空です（開発BOTが起動しません）",
    });
  }

  return issues;
}
