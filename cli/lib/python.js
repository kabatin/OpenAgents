/**
 * 使う Python を見つける。
 *
 * ここでは**バージョンしか見ない**。証明書・Node・依存の検査は start.py が
 * すでにやっている（start.py の check_certificates / check_node / ensure_venv）。
 * 同じ検査を2箇所に書くと、片方だけ直して食い違う。
 */

import fs from "node:fs";
import path from "node:path";
import { capture } from "./proc.js";

/** start.py の MIN_PYTHON と同じ値。片方を変えたらもう片方も見ること */
export const MIN_PYTHON = [3, 10];

const IS_WINDOWS = process.platform === "win32";

/**
 * 候補の並び。Windows の `py -3` を最後に置くのは、
 * python.exe が Microsoft Store のダミー（実行すると Store が開くだけ）である
 * ことがあるため — その場合 `-c` が失敗するので、次の候補へ落ちる。
 */
export function candidates(platform = process.platform) {
  return platform === "win32"
    ? [
        ["python", []],
        ["python3", []],
        ["py", ["-3"]],
      ]
    : [
        ["python3", []],
        ["python", []],
      ];
}

/** "3.12.4" や "3.10" を [3, 12] にする。読めなければ null */
export function parseVersion(text) {
  const match = /(\d+)\.(\d+)/.exec(String(text || ""));
  if (!match) return null;
  return [Number(match[1]), Number(match[2])];
}

/** version が minimum 以上か */
export function meets(version, minimum = MIN_PYTHON) {
  if (!version) return false;
  const [major, minor] = version;
  const [minMajor, minMinor] = minimum;
  if (major !== minMajor) return major > minMajor;
  return minor >= minMinor;
}

/**
 * 条件を満たす Python を探す。見つからなければ null。
 *
 * 戻り値: { command, args, version } — 実行は `command` に
 * `[...args, ...実際の引数]` を渡す（`py -3 script.py` の形に対応するため）。
 */
export function findPython(platform = process.platform) {
  let best = null; // 見つかったが古い、を覚えておいて案内に使う
  for (const [command, args] of candidates(platform)) {
    const probe = capture(
      command,
      [...args, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
      { timeout: 30000 },
    );
    if (!probe.ok) continue;
    const version = parseVersion(probe.stdout);
    if (!version) continue;
    if (meets(version)) return { command, args, version };
    if (!best) best = { command, args, version };
  }
  return best && !meets(best.version) ? { ...best, tooOld: true } : null;
}

/**
 * checkout の venv にある Python。無ければ null。
 *
 * core/paths.py の venv_python() と同じ場所を見ている。
 * 常駐（run.py）は依存が入った venv 側で動かす必要がある。
 */
export function venvPython(home, exists = fs.existsSync) {
  const candidatePaths = [
    path.join(home, "venv", "Scripts", "python.exe"),
    path.join(home, "venv", "bin", "python"),
  ];
  for (const candidate of candidatePaths) {
    if (exists(candidate)) return candidate;
  }
  return null;
}

export const PYTHON_HOW_TO_FIX = IS_WINDOWS
  ? "  https://www.python.org/downloads/ から入れて、ターミナルを開き直してください。\n" +
    "  インストーラの「Add python.exe to PATH」に必ずチェックを入れてください。"
  : "  https://www.python.org/downloads/ から入れて、ターミナルを開き直してください。";
