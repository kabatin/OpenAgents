/**
 * この環境の Python を見つける。
 *
 * venv の中身は Windows と mac/Linux で場所が違う
 * （Scripts\python.exe と bin/python）。Python 側は core/paths.py が
 * 吸収しているので、こちらも同じ順序で探す。
 */
import fs from "node:fs";
import path from "node:path";

import { ROOT_DIR } from "../paths.ts";

const CANDIDATES = [
  path.join("venv", "Scripts", "python.exe"), // Windows
  path.join("venv", "bin", "python"), // mac / Linux
  path.join("venv", "bin", "python3"),
];

/** venv の Python。無ければシステムの python3（Windows なら python）。 */
export function venvPython(): string {
  for (const rel of CANDIDATES) {
    const full = path.join(ROOT_DIR, rel);
    if (fs.existsSync(full)) return full;
  }
  return process.platform === "win32" ? "python" : "python3";
}
