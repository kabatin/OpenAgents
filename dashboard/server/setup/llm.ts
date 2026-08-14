/**
 * 使うAI（LLM）の検出と応答テスト。
 *
 * 判定そのものは Python 側（core/llm.py）が持っている。ここで同じ判定を
 * TypeScript で書き直すと、**画面とBOTで違うことを言い出す**ので、
 * 判定は必ず Python に聞く。ここはその橋渡しだけ。
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { ROOT_DIR } from "../paths.ts";
import { venvPython } from "./python.ts";

const run = promisify(execFile);

const DETECT_TIMEOUT_MS = 15_000;
/** 応答テストは実際に生成させるので長めに待つ */
const TEST_TIMEOUT_MS = 180_000;

export type ProviderInfo = {
  name: string;
  label: string;
  installed: boolean;
  path: string;
  install: string;
  /** ツールを使う機能（添付読解・Web検索等）に対応しているか */
  rich: boolean;
  defaultModel: string;
};

async function callPython(code: string, timeoutMs: number): Promise<unknown> {
  const { stdout } = await run(venvPython(), ["-c", code], {
    cwd: ROOT_DIR,
    timeout: timeoutMs,
    maxBuffer: 4 * 1024 * 1024,
    env: { ...process.env, PYTHONPATH: ROOT_DIR, PYTHONIOENCODING: "utf-8" },
  });
  // Python 側は最終行に JSON だけを出す（BOTの print が混ざっても壊れないように）
  const lines = stdout.trim().split("\n");
  const last = lines[lines.length - 1] ?? "{}";
  return JSON.parse(last);
}

const DETECT_CODE = `
import json
from core import config, llm
cfg = config.load()
out = []
for p in llm.detect_available(cfg):
    out.append({
        "name": p["name"], "label": p["label"], "installed": p["installed"],
        "path": p["path"], "install": p["install"], "rich": p["rich"],
        "defaultModel": p["default_model"],
    })
print(json.dumps({"providers": out, "selected": llm.selected(cfg)}, ensure_ascii=False))
`;

/** インストール済みのAIツールを調べる。 */
export async function detectProviders(): Promise<{
  providers: ProviderInfo[];
  selected: string;
}> {
  const got = (await callPython(DETECT_CODE, DETECT_TIMEOUT_MS)) as {
    providers: ProviderInfo[];
    selected: string;
  };
  return got;
}

/**
 * 実際に一言返させる。
 * 「設定できた気がする」で終わらせず、目に見える形で確かめてもらう。
 */
export async function testProvider(
  provider: string,
  model: string,
): Promise<{ ok: boolean; reply: string; limits: string; error: string }> {
  const code = `
import json
from core import config, llm
cfg = config.load()
cfg.setdefault("llm", {})
cfg["llm"]["provider"] = ${JSON.stringify(provider)}
${model ? `cfg["llm"]["model"] = ${JSON.stringify(model)}` : ""}
result = {"ok": False, "reply": "", "limits": "", "error": ""}
try:
    result["limits"] = llm.describe_limits(cfg)
    result["reply"] = llm.generate(
        "「準備できました」とだけ返してください。他には何も書かないでください。", cfg)
    result["ok"] = True
except Exception as e:
    result["error"] = str(e)[:500]
print(json.dumps(result, ensure_ascii=False))
`;
  try {
    return (await callPython(code, TEST_TIMEOUT_MS)) as {
      ok: boolean;
      reply: string;
      limits: string;
      error: string;
    };
  } catch (e) {
    const cause = e instanceof Error ? e.message : String(e);
    return { ok: false, reply: "", limits: "", error: `実行できませんでした: ${cause}` };
  }
}

/** 選んだプロバイダで使えない機能の説明（問題なければ空文字）。 */
export async function describeLimits(provider: string): Promise<string> {
  const code = `
import json
from core import config, llm
cfg = config.load()
cfg.setdefault("llm", {})
cfg["llm"]["provider"] = ${JSON.stringify(provider)}
try:
    print(json.dumps({"limits": llm.describe_limits(cfg)}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"limits": "", "error": str(e)[:300]}, ensure_ascii=False))
`;
  const got = (await callPython(code, DETECT_TIMEOUT_MS)) as { limits?: string };
  return got.limits ?? "";
}
