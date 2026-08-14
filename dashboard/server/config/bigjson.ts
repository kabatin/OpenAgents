/**
 * DiscordのスノーフレークID を壊さない JSON の読み書き。
 *
 * config.json には `agent_category_id: 1234567890123456789` のように、
 * **引用符なしの19桁の整数**が入っている。JavaScriptの number は
 * 2^53-1（16桁）までしか正確に持てないので、素朴に JSON.parse → JSON.stringify すると
 * 1234567890123456789 が 1234567890123456800 に化ける。
 * 設定エディタが黙ってIDを書き換えるのは最悪の事故なので、ここで防ぐ。
 *
 * 手口: 安全でない整数は「原文の数字列」を持つ目印つき文字列として読み込み、
 * 書き出す直前に目印を外して**引用符なしの数値**へ戻す。
 * こうすると JSON の型（number のまま）も桁も完全に保たれる。
 */

/** JSONの文字列内に自然には現れない制御文字を目印に使う。 */
const RAW_PREFIX = "\u0000bigint:";
const RAW_IN_JSON = /"\\u0000bigint:(-?\d+)"/g;
const INTEGER_SOURCE = /^-?\d+$/;

/** 目印つきの値か（画面表示や書き込み時の判定に使う）。 */
export function isRawBigInt(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(RAW_PREFIX);
}

/** 目印を外して人が読める数字列にする。 */
export function rawBigIntText(value: string): string {
  return value.slice(RAW_PREFIX.length);
}

/** 表示用: 目印つきならただの数字列に、それ以外はそのまま。 */
export function displayValue(value: unknown): unknown {
  return isRawBigInt(value) ? rawBigIntText(value) : value;
}

/**
 * 精度を落とさずに読む。
 * 安全な整数に収まらない数値だけを目印つき文字列にして持ち回る。
 */
export function parseJson(text: string): unknown {
  return JSON.parse(text, function reviver(_key: string, value: unknown, context?: { source?: string }) {
    if (
      typeof value === "number" &&
      !Number.isSafeInteger(value) &&
      context?.source !== undefined &&
      INTEGER_SOURCE.test(context.source)
    ) {
      return RAW_PREFIX + context.source;
    }
    return value;
  } as (key: string, value: unknown) => unknown);
}

/** 読んだときの桁のまま書き出す（目印つきは引用符なしの数値に戻る）。 */
export function stringifyJson(value: unknown): string {
  return JSON.stringify(value, null, 2).replace(RAW_IN_JSON, "$1");
}
