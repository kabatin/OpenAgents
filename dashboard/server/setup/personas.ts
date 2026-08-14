/**
 * 性格ファイル（personas/）と前提知識（knowledge/）の読み書き。
 *
 * 画面から編集できるようにするが、**書ける場所はこの2つのフォルダに限る**。
 * パスを受け取ってファイルを書くAPIは、パストラバーサル（`../../.ssh/...`）で
 * どこにでも書けてしまう典型なので、正規化してから接頭辞を検査する。
 */
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

import { KNOWLEDGE_DIR, PERSONAS_DIR } from "../paths.ts";

export class PersonaError extends Error {}

export type PersonaFile = {
  /** リポジトリルートからの相対パス（config.json に書く形） */
  relPath: string;
  /** ファイル名（拡張子込み） */
  fileName: string;
  /** テンプレートかどうか（テンプレートは上書きさせない） */
  isTemplate: boolean;
  /** フロントマターの name（無ければファイル名） */
  title: string;
  /** フロントマターの summary */
  summary: string;
  /** 差し込み語（{{AGENT_NAME}} 等） */
  placeholders: string[];
};

const AREAS = {
  personas: PERSONAS_DIR,
  knowledge: KNOWLEDGE_DIR,
} as const;

export type AreaName = keyof typeof AREAS;

/**
 * 書き込み先の絶対パスを安全に解決する。
 * `..` を含む・拡張子が .md でない・対象フォルダの外を指す、のいずれも拒否。
 */
export function resolveSafe(area: AreaName, fileName: string): string {
  const base = AREAS[area];
  if (base === undefined) throw new PersonaError(`不明な場所です: ${area}`);
  if (!fileName || fileName.includes("/") || fileName.includes("\\")) {
    throw new PersonaError("ファイル名にフォルダは指定できません");
  }
  if (!fileName.endsWith(".md")) {
    throw new PersonaError("ファイル名は .md で終わる必要があります");
  }
  const full = path.resolve(base, fileName);
  // 正規化した結果が対象フォルダの直下でなければ拒否（`..` 対策）
  if (path.dirname(full) !== path.resolve(base)) {
    throw new PersonaError("そのパスには書き込めません");
  }
  return full;
}

/** 冒頭の YAML フロントマターを緩く読む（ライブラリを足すほどではない）。 */
export function parseFrontMatter(text: string): {
  meta: Record<string, string | string[]>;
  body: string;
} {
  if (!text.startsWith("---")) return { meta: {}, body: text };
  const end = text.indexOf("\n---", 3);
  if (end === -1) return { meta: {}, body: text };
  const head = text.slice(3, end).trim();
  const body = text.slice(end + 4).replace(/^\n/, "");
  const meta: Record<string, string | string[]> = {};
  for (const line of head.split("\n")) {
    const idx = line.indexOf(":");
    if (idx === -1) continue;
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if (value.startsWith("[") && value.endsWith("]")) {
      meta[key] = value
        .slice(1, -1)
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean);
      continue;
    }
    value = value.replace(/^["']|["']$/g, "");
    meta[key] = value;
  }
  return { meta, body };
}

function describe(area: AreaName, fileName: string): PersonaFile {
  const full = path.join(AREAS[area], fileName);
  let text = "";
  try {
    text = fs.readFileSync(full, "utf8");
  } catch {
    text = "";
  }
  const { meta } = parseFrontMatter(text);
  const isTemplate = fileName.endsWith(".template.md") || fileName.endsWith(".example.md");
  return {
    relPath: `${area}/${fileName}`,
    fileName,
    isTemplate,
    title: (meta["name"] as string) || fileName.replace(/\.md$/, ""),
    summary: (meta["summary"] as string) || "",
    placeholders: (meta["placeholders"] as string[]) ?? [],
  };
}

/** そのフォルダにある .md の一覧（README は除く）。 */
export function list(area: AreaName): PersonaFile[] {
  const base = AREAS[area];
  let names: string[] = [];
  try {
    names = fs.readdirSync(base);
  } catch {
    return [];
  }
  return names
    .filter((n) => n.endsWith(".md") && n.toLowerCase() !== "readme.md")
    .sort()
    .map((n) => describe(area, n));
}

export function read(area: AreaName, fileName: string): string {
  const full = resolveSafe(area, fileName);
  try {
    return fs.readFileSync(full, "utf8");
  } catch {
    throw new PersonaError(`${fileName} を読めませんでした`);
  }
}

/** 保存する。テンプレートは上書きさせない（元に戻せなくなるため）。 */
export async function write(area: AreaName, fileName: string, content: string): Promise<void> {
  const full = resolveSafe(area, fileName);
  if (fileName.endsWith(".template.md") || fileName.endsWith(".example.md")) {
    throw new PersonaError(
      "同梱のテンプレートは上書きできません。別の名前で保存してください",
    );
  }
  if (content.length > 200_000) {
    throw new PersonaError("内容が大きすぎます（20万文字まで）");
  }
  await fsp.mkdir(path.dirname(full), { recursive: true });
  const tmp = `${full}.tmp`;
  await fsp.writeFile(tmp, content, "utf8");
  await fsp.rename(tmp, full);
}

/**
 * テンプレートから新しい性格ファイルを作る。
 * `{{PLACEHOLDER}}` を置き換えてから保存する。
 */
export async function createFromTemplate(
  area: AreaName,
  templateName: string,
  newFileName: string,
  values: Record<string, string>,
): Promise<PersonaFile> {
  const source = read(area, templateName);
  const { body } = parseFrontMatter(source);
  let text = body;
  for (const [key, value] of Object.entries(values)) {
    text = text.split(`{{${key}}}`).join(value);
  }
  await write(area, newFileName, text);
  return describe(area, newFileName);
}
