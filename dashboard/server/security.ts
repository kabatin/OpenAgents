/**
 * ローカルネットワークに開くための最低限の防御。
 *
 * 前提: この画面は設定を書き換えBOTを再起動できる。認証は既定で無い。
 * 守るべきなのは「同じLANにいる人」よりも、むしろ
 * **利用者自身が開いた悪意あるWebページ**からの操作である。
 *
 *   悪意あるページが fetch("http://192.168.0.42:8787/api/ops/.../restart", {method:"POST"})
 *   を投げると、CORSは*レスポンスの読み取り*しか止めないので、
 *   リクエストは届いてBOTが再起動してしまう。パスワードでは防げない
 *   （ブラウザが勝手に認証情報を付けて送るため）。
 *
 * そこで2段構えにする:
 *   1. Host検査  … DNSリバインディング対策（攻撃者のドメイン名で来たら拒否）
 *   2. CSRF検査  … 別オリジン発のリクエストを拒否（上記の本命の対策）
 *
 * 判定はすべて純粋関数にしてテストする（tests/security.test.ts）。
 */

/** 状態を変えるメソッド。GET/HEAD は副作用が無いので検査しない。 */
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

const PRIVATE_IPV4 =
  /^(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/;

const LOOPBACK = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

/** `example.com:8787` → `example.com` / `[::1]:8787` → `[::1]` */
export function hostnameOf(host: string): string {
  if (host.startsWith("[")) {
    const end = host.indexOf("]");
    return end === -1 ? host : host.slice(0, end + 1);
  }
  const colon = host.lastIndexOf(":");
  return colon === -1 ? host : host.slice(0, colon);
}

/**
 * このHostヘッダでのアクセスを許すか。
 *
 * 許すのは「自分のマシン」「同じLAN」「.local(Bonjour)」と、明示的に足したホストだけ。
 * **公開ドメイン名で来たら拒否する** — これがDNSリバインディング対策の本体で、
 * 攻撃者が evil.com を 192.168.0.42 に向けても Host が evil.com なので弾ける。
 */
export function isAllowedHost(host: string | undefined, extra: string[] = []): boolean {
  if (host === undefined || host === "") return false;
  if (extra.includes(host)) return true;

  const name = hostnameOf(host).toLowerCase();
  if (extra.includes(name)) return true;
  if (LOOPBACK.has(name)) return true;
  if (PRIVATE_IPV4.test(name)) return true;
  if (name.endsWith(".local")) return true;
  // Tailscale の MagicDNS（*.ts.net）。使う場合はここが効く
  if (name.endsWith(".ts.net")) return true;
  return false;
}

export type CsrfVerdict = { ok: true } | { ok: false; reason: string };

/**
 * 別オリジンからの状態変更を拒否する。
 *
 * - `Sec-Fetch-Site`: 現代のブラウザが必ず付ける。same-origin 以外は拒否。
 * - `Origin`: フォーム送信や fetch が付ける。Host と一致しなければ拒否。
 *
 * どちらのヘッダも無い場合は通す（curl や launchd からの操作は正当）。
 * 攻撃はブラウザ経由でしか成立せず、ブラウザは必ずどちらかを付けるため、
 * これで穴は塞がる。
 */
export function checkCsrf(req: {
  method: string;
  host?: string | undefined;
  origin?: string | undefined;
  secFetchSite?: string | undefined;
}): CsrfVerdict {
  if (!MUTATING_METHODS.has(req.method.toUpperCase())) return { ok: true };

  const site = req.secFetchSite;
  if (site !== undefined && site !== "same-origin" && site !== "none") {
    return { ok: false, reason: `別サイトからの操作は受け付けません（Sec-Fetch-Site: ${site}）` };
  }

  const origin = req.origin;
  if (origin !== undefined && origin !== "null") {
    let originHost: string;
    try {
      originHost = new URL(origin).host;
    } catch {
      return { ok: false, reason: `Origin ヘッダが不正です（${origin}）` };
    }
    if (req.host === undefined || originHost.toLowerCase() !== req.host.toLowerCase()) {
      return { ok: false, reason: `別オリジンからの操作は受け付けません（${origin}）` };
    }
  }

  return { ok: true };
}

/** 環境変数から追加の許可ホストを読む（カンマ区切り）。 */
export function extraAllowedHosts(raw: string | undefined): string[] {
  if (raw === undefined || raw.trim() === "") return [];
  return raw
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter((s) => s.length > 0);
}
