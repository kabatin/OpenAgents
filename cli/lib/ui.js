/**
 * 画面に出す言葉を1箇所に集める。
 *
 * 語り口は start.py の say()/fail() に合わせてある。npm から入った人と
 * git clone から入った人で、同じ失敗が違う顔で出てくると混乱するため。
 */

export function say(message = "") {
  process.stdout.write(`${message}\n`);
}

export function heading(title) {
  say("=".repeat(60));
  say(`  ${title}`);
  say("=".repeat(60));
}

/**
 * 表示上の幅（半角=1・全角=2）。
 *
 * String.padEnd は UTF-16 の個数で数えるので、日本語のラベルを並べると
 * 桁が揃わない。表の見た目だけの話だが、揃っていないと読み飛ばされる。
 */
export function displayWidth(text) {
  let width = 0;
  for (const char of String(text)) {
    width += isWide(char.codePointAt(0)) ? 2 : 1;
  }
  return width;
}

function isWide(code) {
  return (
    (code >= 0x1100 && code <= 0x115f) || // ハングル字母
    (code >= 0x2e80 && code <= 0xa4cf) || // CJK 部首〜漢字・かな
    (code >= 0xac00 && code <= 0xd7a3) || // ハングル音節
    (code >= 0xf900 && code <= 0xfaff) || // CJK 互換漢字
    (code >= 0xfe30 && code <= 0xfe6f) || // CJK 互換記号
    (code >= 0xff00 && code <= 0xff60) || // 全角英数・記号
    (code >= 0xffe0 && code <= 0xffe6)
  );
}

/** 表示幅を揃えて右に詰める */
export function padDisplay(text, width) {
  const padding = width - displayWidth(text);
  return padding > 0 ? `${text}${" ".repeat(padding)}` : String(text);
}

/**
 * 失敗を伝えて終わる。
 *
 * **理由だけを出して終わらない。** 何をすれば直るかを必ず添える
 * （添えられないなら、それは検査として未完成という意味）。
 */
export function fail(message, howToFix = "") {
  say();
  say(`❌ ${message}`);
  if (howToFix) {
    say();
    say(howToFix);
  }
  say();
  process.exit(1);
}
