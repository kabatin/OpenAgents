// ヘッドレス Chrome を DevTools Protocol で直接操作してスクショを撮る（依存ゼロ）。
// 使い方: node capture.mjs <DevToolsのWebSocket URL> <ダッシュボードのURL> <出力dir>
// 各ショットは「中身の外接矩形＋余白」で切り出すので、空白の多い画像にならない。
import fs from "node:fs";
import path from "node:path";

const [wsUrl, base, outDir] = process.argv.slice(2);
const WIDTH = 1180;
const HEIGHT = 900;
const SCALE = 2;
const PAD = 24;

const ws = new WebSocket(wsUrl);
let seq = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const { resolve, reject } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
  }
};
await new Promise((r) => (ws.onopen = r));

function send(method, params = {}, sessionId) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params, sessionId }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const { targetId } = await send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
const cmd = (m, p) => send(m, p, sessionId);
await cmd("Page.enable");
await cmd("Runtime.enable");
await cmd("Emulation.setDeviceMetricsOverride", {
  width: WIDTH, height: HEIGHT, deviceScaleFactor: SCALE, mobile: false,
});
await cmd("Emulation.setEmulatedMedia", {
  features: [{ name: "prefers-color-scheme", value: "light" }],
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function evaluate(expr) {
  const r = await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? "eval error");
  return r.result.value;
}

// 文字列でボタン等を押す（画面の文言が変わっても探せるように部分一致）
const click = (text, sel = "main button, main a, main [role=tab]") => evaluate(`(() => {
  const el = [...document.querySelectorAll(${JSON.stringify(sel)})]
    .find((e) => e.innerText && e.innerText.includes(${JSON.stringify(text)}));
  if (el) el.click();
  return !!el;
})()`);

// 「はじめての方へ」の案内は閉じてから撮る（同じ案内が全画像に写らないように）
const closeGuides = () => evaluate(`(() => {
  for (const b of document.querySelectorAll("main button")) {
    if (b.innerText.trim() === "閉じる") b.click();
  }
})()`);

async function shoot(name, url, { prepare, selector = "main", maxHeight = 1500, header = true, stopBefore } = {}) {
  await cmd("Page.navigate", { url: base + url });
  await sleep(2500);
  await closeGuides();
  if (prepare) await prepare();
  await sleep(1200);
  // ヘッダー（ナビ）と本文を含む矩形。selector 内の実際の子要素の外接で切る
  const box = await evaluate(`(() => {
    const root = document.querySelector(${JSON.stringify(selector)}) || document.body;
    const nodes = [root, ...root.querySelectorAll("*")].filter((n) => {
      const r = n.getBoundingClientRect();
      const s = getComputedStyle(n);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
    });
    let x1 = Infinity, y1 = Infinity, x2 = 0, y2 = 0;
    for (const n of nodes) {
      if (n === root) continue;
      const r = n.getBoundingClientRect();
      x1 = Math.min(x1, r.left); y1 = Math.min(y1, r.top + scrollY);
      x2 = Math.max(x2, r.right); y2 = Math.max(y2, r.bottom + scrollY);
    }
    // ナビも写すショットは上端から（どの画面かが分かるように）
    const hdr = document.querySelector("header");
    if (${header ? "true" : "false"} && hdr) y1 = 0;
    // stopBefore の見出しを持つカードの手前で切る（下の方の縦長パネルを写さない）
    const stop = ${JSON.stringify(stopBefore ?? null)};
    if (stop) {
      const h = [...document.querySelectorAll("main h2, main h3")].find((e) => e.innerText.trim() === stop);
      const card = h && (h.closest("section, article, .card, [class*=rounded]") || h);
      if (card) y2 = Math.min(y2, card.getBoundingClientRect().top + scrollY - ${PAD} - 4);
    }
    return { x1, y1, x2, y2, docH: document.documentElement.scrollHeight };
  })()`);
  const x = Math.max(0, box.x1 - PAD);
  const y = Math.max(0, box.y1 - (box.y1 === 0 ? 0 : PAD));
  const w = Math.min(WIDTH, box.x2 + PAD) - x;
  const h = Math.min(box.y2 + PAD - y, maxHeight);
  // 全体を描画できる高さまで一時的に伸ばす
  await cmd("Emulation.setDeviceMetricsOverride", {
    width: WIDTH, height: Math.ceil(Math.max(HEIGHT, y + h)), deviceScaleFactor: SCALE, mobile: false,
  });
  await sleep(600);
  const shot = await cmd("Page.captureScreenshot", {
    format: "png", clip: { x, y, width: w, height: h, scale: 1 }, captureBeyondViewport: true,
  });
  fs.writeFileSync(path.join(outDir, `${name}.png`), Buffer.from(shot.data, "base64"));
  await cmd("Emulation.setDeviceMetricsOverride", {
    width: WIDTH, height: HEIGHT, deviceScaleFactor: SCALE, mobile: false,
  });
  console.log(`${name}: ${Math.round(w)}x${Math.round(h)} css px`);
}

await shoot("setup-wizard", "/setup", { header: false });
await shoot("overview", "/", { maxHeight: 1250 });
await shoot("settings", "/agents/akari", {
  prepare: async () => { await click("よく使う設定"); },
  maxHeight: 1250,
});
await shoot("personas", "/personas", {
  prepare: async () => { await click("akari.md"); },
  maxHeight: 1100,
});
await shoot("ops", "/ops", { maxHeight: 1300, stopBefore: "ログ" });
await shoot("data", "/data#tasks", { maxHeight: 1000 });

await send("Target.closeTarget", { targetId });
ws.close();
