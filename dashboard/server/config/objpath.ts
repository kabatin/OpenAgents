/**
 * ドット区切りパスでのオブジェクト読み書き（すべて非破壊）。
 *
 * config.json は人間が手で育ててきたファイルで、`_comment` のような
 * コードが読まない鍵も入っている。**型付きオブジェクトから作り直すのではなく、
 * 読んだJSONの該当パスだけを差し替える**ことで、未知の鍵とキー順を保存する。
 */

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

function isPlainObject(v: unknown): v is Record<string, Json> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function splitPath(path: string): string[] {
  return path.split(".").filter((s) => s.length > 0);
}

/**
 * パスの1区間が配列の添字として使えるか。
 * **配列を配列のまま扱うのが要**（`agents.0.name` で agents をオブジェクトに
 * 化けさせると、BOTは起動時に `agents` を配列として読むので即死する）。
 */
function arrayIndex(container: unknown, key: string): number | null {
  if (!Array.isArray(container)) return null;
  if (!/^\d+$/.test(key)) return null;
  return Number.parseInt(key, 10);
}

/** 値を取り出す。途中が無ければ undefined。配列は数値添字で辿る。 */
export function getPath(root: unknown, path: string): unknown {
  let cur: unknown = root;
  for (const key of splitPath(path)) {
    const idx = arrayIndex(cur, key);
    if (idx !== null) {
      cur = (cur as unknown[])[idx];
      continue;
    }
    if (!isPlainObject(cur)) return undefined;
    cur = cur[key];
  }
  return cur;
}

/**
 * 値を設定した新しいオブジェクト／配列を返す（元は変更しない）。
 * 配列は配列のまま複製し、オブジェクトは既存のキー順を保つ。
 * 途中が無ければオブジェクトとして作る（配列を勝手に生やさない）。
 */
export function setPath<T>(root: T, path: string, value: unknown): T {
  const keys = splitPath(path);
  if (keys.length === 0) return value as T;
  const [head, ...rest] = keys as [string, ...string[]];

  const idx = arrayIndex(root, head);
  if (idx !== null) {
    const copy = [...(root as unknown[])];
    copy[idx] = rest.length === 0 ? value : setPath(copy[idx], rest.join("."), value);
    return copy as T;
  }

  const base: Record<string, unknown> = isPlainObject(root)
    ? { ...(root as Record<string, Json>) }
    : {};
  base[head] = rest.length === 0 ? value : setPath(base[head], rest.join("."), value);
  return base as T;
}

/** キーを消した新しいオブジェクトを返す（元は変更しない）。配列要素は消さない。 */
export function deletePath<T>(root: T, path: string): T {
  const keys = splitPath(path);
  if (keys.length === 0) return root;
  const [head, ...rest] = keys as [string, ...string[]];

  const idx = arrayIndex(root, head);
  if (idx !== null) {
    if (rest.length === 0) return root; // 添字を消して詰めると別物になるので拒否
    const copy = [...(root as unknown[])];
    copy[idx] = deletePath(copy[idx], rest.join("."));
    return copy as T;
  }

  if (!isPlainObject(root)) return root;
  if (!(head in root)) return root;
  const base: Record<string, unknown> = { ...(root as Record<string, Json>) };
  if (rest.length === 0) {
    delete base[head];
  } else {
    base[head] = deletePath(base[head], rest.join("."));
  }
  return base as T;
}

/** 複数のパッチをまとめて適用する。 */
export function applyPatches<T>(root: T, patches: { path: string; value: unknown }[]): T {
  return patches.reduce<T>((acc, p) => setPath(acc, p.path, p.value), root);
}

export type Diff = { path: string; before: unknown; after: unknown };

/**
 * 2つのJSONを比べて、葉レベルの差分の一覧を返す。
 *
 * 値としての配列（`admins` や `exclude_channel_ids`）は「まるごと1つの値」として扱う。
 * ただし `agents` のようなオブジェクトの配列は要素ごとに潜る — でないと
 * 「1体の1設定を変えた」だけで巨大な配列まるごとが差分として出てしまい読めない。
 */
export function diffJson(before: unknown, after: unknown, prefix = ""): Diff[] {
  if (isPlainObject(before) && isPlainObject(after)) {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
    return keys.flatMap((k) => diffJson(before[k], after[k], prefix ? `${prefix}.${k}` : k));
  }
  if (
    Array.isArray(before) &&
    Array.isArray(after) &&
    before.length === after.length &&
    before.every(isPlainObject) &&
    after.every(isPlainObject)
  ) {
    return before.flatMap((el, i) => diffJson(el, after[i], `${prefix}.${i}`));
  }
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  return [{ path: prefix, before, after }];
}

/**
 * 配列の末尾に要素を足した新しい構造を返す（元は変更しない）。
 * エージェントを1体増やすときに使う。path が配列でなければ新しく配列を作る。
 */
export function appendTo<T>(root: T, path: string, value: unknown): T {
  const current = getPath(root, path);
  const list = Array.isArray(current) ? [...current, value] : [value];
  return setPath(root, path, list);
}

/**
 * 配列から要素を1つ取り除いた新しい構造を返す（元は変更しない）。
 * 添字ではなく **一致条件**で消す — 添字で消すと、画面を開いてから
 * 押すまでの間に並びが変わっていた場合に別のものを消してしまう。
 */
export function removeFrom<T>(
  root: T,
  path: string,
  match: (item: unknown) => boolean,
): { next: T; removed: number } {
  const current = getPath(root, path);
  if (!Array.isArray(current)) return { next: root, removed: 0 };
  const kept = current.filter((item) => !match(item));
  return { next: setPath(root, path, kept), removed: current.length - kept.length };
}
