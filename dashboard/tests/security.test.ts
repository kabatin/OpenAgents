import { describe, expect, it } from "vitest";

import {
  checkCsrf,
  extraAllowedHosts,
  hostnameOf,
  isAllowedHost,
} from "../server/security.ts";

describe("hostnameOf", () => {
  it("ポートを落とす", () => {
    expect(hostnameOf("192.168.0.42:8787")).toBe("192.168.0.42");
    expect(hostnameOf("localhost")).toBe("localhost");
  });
  it("IPv6のブラケット表記を扱える", () => {
    expect(hostnameOf("[::1]:8787")).toBe("[::1]");
  });
});

describe("isAllowedHost", () => {
  it("自分のマシンは許す", () => {
    for (const h of ["localhost:8787", "127.0.0.1:8787", "[::1]:8787", "localhost:5173"]) {
      expect(isAllowedHost(h), h).toBe(true);
    }
  });

  it("同一LANのプライベートIPは許す", () => {
    for (const h of ["192.168.0.42:8787", "10.0.1.5:8787", "172.16.3.9:8787"]) {
      expect(isAllowedHost(h), h).toBe(true);
    }
  });

  it("Bonjour(.local)とTailscale(*.ts.net)は許す", () => {
    expect(isAllowedHost("my-mac.local:8787")).toBe(true);
    expect(isAllowedHost("mac.tail1234.ts.net")).toBe(true);
  });

  // DNSリバインディング対策の本体。攻撃者のドメインを 192.168.0.42 に
  // 向けられても、Host が公開ドメイン名なのでここで弾ける。
  it("公開ドメイン名は拒否する", () => {
    for (const h of ["evil.com:8787", "dashboard.example.jp", "192-168-0-42.nip.io:8787"]) {
      expect(isAllowedHost(h), h).toBe(false);
    }
  });

  it("グローバルIPは拒否する", () => {
    expect(isAllowedHost("203.0.113.7:8787")).toBe(false);
    expect(isAllowedHost("172.32.0.1:8787")).toBe(false); // プライベート範囲の外側（境界）
    expect(isAllowedHost("172.15.0.1:8787")).toBe(false);
  });

  it("プライベート範囲の境界を正しく扱う", () => {
    expect(isAllowedHost("172.16.0.1:8787")).toBe(true);
    expect(isAllowedHost("172.31.255.254:8787")).toBe(true);
  });

  it("Hostが無ければ拒否", () => {
    expect(isAllowedHost(undefined)).toBe(false);
    expect(isAllowedHost("")).toBe(false);
  });

  it("明示的に足したホストは許す", () => {
    expect(isAllowedHost("dash.example.com:8787", ["dash.example.com:8787"])).toBe(true);
    expect(isAllowedHost("dash.example.com:8787", ["dash.example.com"])).toBe(true);
  });
});

describe("checkCsrf", () => {
  const host = "192.168.0.42:8787";

  it("GET は検査しない（副作用が無い）", () => {
    expect(checkCsrf({ method: "GET", host, secFetchSite: "cross-site" }).ok).toBe(true);
  });

  it("自分の画面からの操作は通る", () => {
    const r = checkCsrf({
      method: "POST",
      host,
      origin: `http://${host}`,
      secFetchSite: "same-origin",
    });
    expect(r.ok).toBe(true);
  });

  // これが今回いちばん防ぎたい攻撃。悪意あるページが
  // fetch("http://192.168.0.42:8787/api/ops/services/archivebot/restart", {method:"POST"})
  // を投げてもここで止まる。
  it("別サイトからの再起動POSTを拒否する", () => {
    const r = checkCsrf({
      method: "POST",
      host,
      origin: "http://evil.example",
      secFetchSite: "cross-site",
    });
    expect(r.ok).toBe(false);
  });

  it("Sec-Fetch-Site が same-site でも拒否（サブドメイン経由を許さない）", () => {
    expect(checkCsrf({ method: "POST", host, secFetchSite: "same-site" }).ok).toBe(false);
  });

  it("Origin だけ食い違う場合も拒否", () => {
    const r = checkCsrf({ method: "POST", host, origin: "http://evil.example" });
    expect(r.ok).toBe(false);
  });

  it("PATCH（設定変更）も同じく守られる", () => {
    expect(
      checkCsrf({ method: "PATCH", host, origin: "http://evil.example", secFetchSite: "cross-site" })
        .ok,
    ).toBe(false);
  });

  it("ヘッダが無い場合は通す（curl や スクリプトからの操作は正当）", () => {
    expect(checkCsrf({ method: "POST", host }).ok).toBe(true);
  });

  it("Origin が不正な文字列なら拒否", () => {
    expect(checkCsrf({ method: "POST", host, origin: "http://" }).ok).toBe(false);
  });

  it("開発時のVite(5173)からの操作は通る", () => {
    const dev = "127.0.0.1:5173";
    const r = checkCsrf({
      method: "PATCH",
      host: dev,
      origin: `http://${dev}`,
      secFetchSite: "same-origin",
    });
    expect(r.ok).toBe(true);
  });
});

describe("extraAllowedHosts", () => {
  it("カンマ区切りを読む", () => {
    expect(extraAllowedHosts(" a.example:8787 , B.example ")).toEqual([
      "a.example:8787",
      "b.example",
    ]);
  });
  it("未設定なら空", () => {
    expect(extraAllowedHosts(undefined)).toEqual([]);
    expect(extraAllowedHosts("")).toEqual([]);
  });
});
