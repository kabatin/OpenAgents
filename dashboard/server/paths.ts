/**
 * 参照する外部ファイルの絶対パスを1箇所に集める。
 *
 * ダッシュボードは既存のPython資産（config.json / archive.db / 各ログ）を
 * 「外から覗く」だけなので、パスの散らばりが最大の壊れやすさになる。
 * ここ以外でパスを組み立てないこと。
 */
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** scripts/dashboard */
export const DASHBOARD_DIR = path.resolve(HERE, "..");
/** リポジトリのルート */
export const ROOT_DIR = path.resolve(DASHBOARD_DIR, "..");

/** プラットフォーム非依存の中核 */
export const CORE_DIR = path.join(ROOT_DIR, "core");
/** Discord 実装 */
export const ARCHIVE_DIR = path.join(ROOT_DIR, "platforms", "discord");
export const DEV_BOT_DIR = path.join(ARCHIVE_DIR, "dev");
export const MEETING_BOT_DIR = path.join(ARCHIVE_DIR, "meeting");

/** 性格ファイル置き場（core/paths.py の PERSONAS_DIR と同じ場所を指すこと） */
export const PERSONAS_DIR = path.join(ROOT_DIR, "personas");
/** 前提知識の置き場 */
export const KNOWLEDGE_DIR = path.join(ROOT_DIR, "knowledge");
/** 外部連携の置き場 */
export const INTEGRATIONS_DIR = path.join(ROOT_DIR, "integrations");

/** 実行時に作られるもの（core/paths.py の STATE_DIR と同じ場所を指すこと） */
export const STATE_DIR = path.join(ROOT_DIR, "state");
export const LOGS_DIR = path.join(STATE_DIR, "logs");
export const HEARTBEAT_DIR = path.join(STATE_DIR, "heartbeat");

/** 設定の唯一の真実（Botトークン入り・gitignore済み）。リポジトリ直下の1枚 */
export const CONFIG_PATH = path.join(ROOT_DIR, "config.json");
/** 同梱の設定例。初期設定モードの雛形であり、テストの検証対象でもある */
export const CONFIG_EXAMPLE_PATH = path.join(ROOT_DIR, "config.example.json");

export const ARCHIVE_DB_PATH = path.join(STATE_DIR, "archive.db");
export const REMINDERS_PATH = path.join(STATE_DIR, "reminders.json");
export const DEV_HEARTBEAT_PATH = path.join(HEARTBEAT_DIR, "devbot");

/** ダッシュボード自身の書き込み先（gitignore対象） */
export const BACKUPS_DIR = path.join(DASHBOARD_DIR, "backups");
export const APPLIED_DIR = path.join(DASHBOARD_DIR, "applied");
export const WEB_DIST_DIR = path.join(DASHBOARD_DIR, "dist");

/** launchctl のドメインターゲット（gui/<uid>）。 */
export const GUI_DOMAIN = `gui/${os.userInfo().uid}`;

export type ServiceId = "archivebot" | "devbot" | "meetingbot";

export type ServiceDef = {
  id: ServiceId;
  label: string;
  /** launchd ジョブ名 */
  launchdLabel: string;
  /** 常駐プロセスか（false = 定期起動なのでPID無しが正常） */
  resident: boolean;
  /** Discordゲートウェイへ繋ぐBotか（lsofでの接続数判定の対象） */
  gateway: boolean;
  stdout: string | null;
  stderr: string | null;
  /**
   * ログの更新が止まったことを異常とみなしてよいか。
   * meetingbot はイベント駆動で数日無音が正常なので false。
   * devbot は heartbeat があるのでログ鮮度は使わない。
   */
  logFreshnessMeaningful: boolean;
  /** このサービスが config.json のどの部分を読むか（未適用差分の割り当てに使う） */
  ownsConfig: "archive" | "dev" | "meeting" | null;
  note?: string;
};

export const SERVICES: ServiceDef[] = [
  {
    id: "archivebot",
    label: "会話エージェント",
    launchdLabel: "com.discord.archivebot",
    resident: true,
    gateway: true,
    stdout: path.join(LOGS_DIR, "archivebot.log"),
    stderr: path.join(LOGS_DIR, "archivebot.error.log"),
    logFreshnessMeaningful: true,
    ownsConfig: "archive",
    note: "全員が1プロセスで動くため、再起動は必ず全員同時になる",
  },
  {
    id: "devbot",
    label: "開発BOT",
    launchdLabel: "com.discord.devbot",
    resident: true,
    gateway: true,
    stdout: path.join(LOGS_DIR, "devbot.log"),
    stderr: path.join(LOGS_DIR, "devbot.error.log"),
    logFreshnessMeaningful: false,
    ownsConfig: "dev",
    note: "heartbeat ファイルが権威的な生存信号（300秒で失効）",
  },
  {
    id: "meetingbot",
    label: "議事録BOT",
    launchdLabel: "com.discord.meetingbot",
    resident: true,
    gateway: true,
    stdout: path.join(LOGS_DIR, "meetingbot.log"),
    stderr: path.join(LOGS_DIR, "meetingbot.error.log"),
    logFreshnessMeaningful: false,
    ownsConfig: "meeting",
    note: "会議が無い間は数日ログが動かないのが正常",
  },
];

export const SERVICE_BY_ID = new Map(SERVICES.map((s) => [s.id, s]));

/** ログビューアが開いてよいファイル（パストラバーサル防止のホワイトリスト） */
export function logTargets(): { id: string; label: string; path: string }[] {
  const out: { id: string; label: string; path: string }[] = [];
  for (const s of SERVICES) {
    if (s.stdout) out.push({ id: `${s.id}:out`, label: `${s.label} / 標準出力`, path: s.stdout });
    if (s.stderr && s.stderr !== s.stdout) {
      out.push({ id: `${s.id}:err`, label: `${s.label} / エラー`, path: s.stderr });
    }
  }
  return out;
}
