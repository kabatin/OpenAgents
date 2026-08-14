/**
 * SQLiteの外にある状態ファイルの読み取り。
 * リマインダーとメール私書箱の対応表はJSONファイルで持たれている。
 */
import fsp from "node:fs/promises";

import { REMINDERS_PATH } from "../paths.ts";

export type Reminder = {
  id: number;
  channel_id: string;
  user_id: string;
  user_name: string;
  content: string;
  due: string;
  repeat: string;
  mention_label: string | null;
  channel_label?: string;
  status: "active" | "done" | "cancelled" | "error";
  fail_count: number;
  created_at: string;
  agent_id: string;
};

export type ReminderSummary = {
  active: number;
  error: number;
  total: number;
  items: Reminder[];
};

export async function readReminders(): Promise<ReminderSummary> {
  try {
    const raw = JSON.parse(await fsp.readFile(REMINDERS_PATH, "utf8")) as {
      reminders?: Reminder[];
    };
    const items = raw.reminders ?? [];
    return {
      active: items.filter((r) => r.status === "active").length,
      error: items.filter((r) => r.status === "error").length,
      total: items.length,
      items,
    };
  } catch {
    return { active: 0, error: 0, total: 0, items: [] };
  }
}

