import { useEffect, useId, useState } from "react";

import { hourLabel, weekdayLabel } from "../lib/format.ts";
import type { ResolvedSetting } from "../lib/types.ts";
import { Button, Chip, Toggle, TriToggle, type TriValue } from "./ui.tsx";

export type SaveFn = (path: string, value: unknown) => Promise<void>;

export type IdNames = Record<string, string>;

/**
 * Discordの生ID（17〜20桁）を名前に置き換える。
 * `1522544734021619764` のままでは、それがどのチャンネルか画面から分からない。
 * 引けなかったIDは生のまま残す（嘘の名前を出さない）。
 */
export function withIdNames(text: string, idNames: IdNames | undefined): string {
  if (idNames === undefined) return text;
  return text.replace(/\d{17,20}/g, (id) => idNames[id] ?? id);
}

/** 行の右端に出す「今どうなっているか」の短い要約。畳んだままでも状態が分かる。 */
function summarize(s: ResolvedSetting, idNames?: IdNames): string | null {
  const v = s.current.value;
  switch (s.kind) {
    case "weekday":
      return weekdayLabel(v);
    case "hour":
      return hourLabel(v);
    case "monthday":
      return typeof v === "number" ? `毎月${v}日` : null;
    case "int":
      return v === null ? null : `${String(v)}${s.unit ?? ""}`;
    case "enum":
      return s.options?.find((o) => o.value === v)?.label ?? null;
    case "stringList":
    case "intList": {
      if (!Array.isArray(v)) return null;
      if (v.length === 0) return "未設定";
      const named = v.map((x) => withIdNames(String(x), idNames));
      const joined = named.join(" / ");
      // 名前に解決できたなら中身を見せる（「2件」だけでは何が入っているか分からない）
      return joined.length <= 40 ? joined : `${v.length}件: ${joined.slice(0, 40)}…`;
    }
    case "string":
    case "text": {
      if (typeof v !== "string" || v.length === 0) return "未設定";
      const shown = withIdNames(v, idNames);
      return shown.length > 26 ? `${shown.slice(0, 26)}…` : shown;
    }
    default:
      return null;
  }
}

/**
 * 既定値から変えているか。config.json に書いてあっても既定と同じ値なら「変更済み」にしない
 * （新人が「どこをいじったのか」だけを拾えるように）。
 */
function isChanged(s: ResolvedSetting): boolean {
  if (!s.current.explicit || s.kind === "info" || s.readonly === true) return false;
  if (s.kind === "tri") {
    const d = (s.default ?? {}) as { enabled?: boolean; shadow?: boolean };
    const def = d.enabled === true ? (d.shadow === false ? "live" : "shadow") : "off";
    return s.current.value !== def;
  }
  if (s.default === undefined) return false;
  return JSON.stringify(s.current.value) !== JSON.stringify(s.default);
}

/** 子パラメータのうち、畳んだ状態でも見せたい要約（曜日・時刻）を組み立てる。 */
function childDigest(s: ResolvedSetting): string | null {
  const parts = (s.children ?? [])
    .filter((c) => c.kind === "weekday" || c.kind === "hour" || c.kind === "monthday")
    .map((c) => summarize(c))
    .filter((x): x is string => x !== null && x !== "—");
  return parts.length > 0 ? parts.join(" ") : null;
}

function ValueEditor({
  setting,
  onSave,
  busy,
  idNames,
}: {
  setting: ResolvedSetting;
  onSave: SaveFn;
  busy: boolean;
  idNames?: IdNames;
}) {
  const [draft, setDraft] = useState<string>(() => {
    const v = setting.current.value;
    if (Array.isArray(v)) return v.join(", ");
    return v === null || v === undefined ? "" : String(v);
  });
  const [dirty, setDirty] = useState(false);

  const commit = async () => {
    let value: unknown = draft.trim();
    if (setting.kind === "stringList") {
      value = draft
        .split(",")
        .map((x) => x.trim())
        .filter((x) => x.length > 0);
    } else if (setting.kind === "intList") {
      value = draft
        .split(",")
        .map((x) => Number(x.trim()))
        .filter((x) => Number.isInteger(x));
    } else if (["int", "hour", "weekday", "monthday"].includes(setting.kind)) {
      value = Number(draft);
    } else if (value === "") {
      value = null;
    }
    try {
      await onSave(setting.path, value);
      setDirty(false);
    } catch {
      // エラー本文は SettingRow が出す。dirty は落とさず「保存」を残す。
    }
  };

  if (setting.readonly === true) {
    const v = setting.current.value;
    const text = Array.isArray(v) ? v.join(" / ") : v === null ? "—" : String(v);
    return <span className="tnum text-xs text-muted">{withIdNames(text, idNames)}</span>;
  }

  if (setting.kind === "enum") {
    return (
      <select
        className="input max-w-[220px]"
        disabled={busy}
        value={String(setting.current.value ?? "")}
        onChange={(e) => {
          const opt = setting.options?.find((o) => String(o.value ?? "") === e.target.value);
          void onSave(setting.path, opt?.value ?? null).catch(() => undefined);
        }}
      >
        {setting.options?.map((o) => (
          <option key={String(o.value)} value={String(o.value ?? "")}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }

  if (setting.kind === "text") {
    return (
      <div className="flex w-full flex-col gap-2">
        <textarea
          className="input min-h-[68px] resize-y leading-relaxed"
          disabled={busy}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            setDirty(true);
          }}
        />
        {dirty && (
          <div className="flex justify-end">
            <Button variant="primary" busy={busy} onClick={() => void commit()}>
              保存
            </Button>
          </div>
        )}
      </div>
    );
  }

  const numeric = ["int", "hour", "weekday", "monthday"].includes(setting.kind);
  const isList = setting.kind === "stringList" || setting.kind === "intList";

  if (setting.kind === "weekday") {
    return (
      <select
        className="input max-w-[120px]"
        disabled={busy}
        value={String(setting.current.value ?? 0)}
        onChange={(e) => void onSave(setting.path, Number(e.target.value)).catch(() => undefined)}
      >
        {[0, 1, 2, 3, 4, 5, 6].map((d) => (
          <option key={d} value={d}>
            {weekdayLabel(d)}
          </option>
        ))}
      </select>
    );
  }

  if (setting.kind === "hour") {
    return (
      <select
        className="input max-w-[110px]"
        disabled={busy}
        value={String(setting.current.value ?? 0)}
        onChange={(e) => void onSave(setting.path, Number(e.target.value)).catch(() => undefined)}
      >
        {Array.from({ length: 24 }, (_, h) => (
          <option key={h} value={h}>
            {hourLabel(h)}
          </option>
        ))}
      </select>
    );
  }

  return (
    <div className="flex w-full items-center gap-2">
      <input
        className={`input ${isList ? "" : "max-w-[220px]"}`}
        type={numeric ? "number" : "text"}
        inputMode={numeric ? "numeric" : undefined}
        min={setting.min}
        max={setting.max}
        disabled={busy}
        value={draft}
        placeholder={isList ? "カンマ区切り" : "未設定"}
        onChange={(e) => {
          setDraft(e.target.value);
          setDirty(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") void commit();
        }}
      />
      {setting.unit !== undefined && <span className="text-2xs text-faint">{setting.unit}</span>}
      {dirty && (
        <Button variant="primary" busy={busy} onClick={() => void commit()}>
          保存
        </Button>
      )}
    </div>
  );
}

export function SettingRow({
  setting,
  onSave,
  depth = 0,
  idNames,
}: {
  setting: ResolvedSetting;
  onSave: SaveFn;
  depth?: number;
  idNames?: IdNames;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const detailId = useId();

  // セレクトやトグルは押した瞬間に保存される。成功の手応えが何も無いと
  // 「効いたのか」が分からないので、短く合図を出す。
  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => setSaved(false), 2500);
    return () => clearTimeout(t);
  }, [saved]);

  const blocked = setting.blockedBy.length > 0;
  const hasDetail =
    (setting.children?.length ?? 0) > 0 ||
    setting.fixedNote !== undefined ||
    blocked ||
    !["bool", "tri"].includes(setting.kind);

  /** トグル等の即時保存用。エラーは行内に出るので、ここでは握って終わる。 */
  const saveQuiet = (path: string, value: unknown): void => {
    void save(path, value).catch(() => undefined);
  };

  const save: SaveFn = async (path, value) => {
    setBusy(true);
    setError(null);
    try {
      await onSave(path, value);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
      // 握りつぶすと ValueEditor が「保存できた」と誤解して dirty を下ろし、
      // 「保存」ボタンが消えて再保存できなくなる（入力値は画面に残ったまま）。
      throw e;
    } finally {
      setBusy(false);
    }
  };

  const isOn =
    setting.kind === "tri"
      ? setting.current.value !== "off"
      : setting.kind === "bool"
        ? setting.current.value === true
        : false;

  const digest = childDigest(setting) ?? summarize(setting, idNames);

  // 行の見出し部分の中身（開閉ボタンにも、開けない行の素の表示にも同じものを使う）
  const head = (
    <>
      <span
        className={`w-3 shrink-0 text-xs text-muted transition-transform duration-150 ${
          hasDetail ? "" : "opacity-0"
        } ${open ? "rotate-90" : ""}`}
        aria-hidden="true"
      >
        ▸
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={`truncate text-sm ${isOn || setting.kind === "tri" ? "font-medium" : ""} ${
              blocked ? "text-muted" : ""
            }`}
          >
            {setting.label}
          </span>
          {isChanged(setting) && <Chip tone="plum">変更済み</Chip>}
          {blocked && <Chip tone="warn">前提が未設定</Chip>}
        </div>
        {!open && setting.desc !== "" && (
          <div className="mt-0.5 line-clamp-1 text-2xs text-muted">{setting.desc}</div>
        )}
      </div>
    </>
  );

  return (
    <div className={depth > 0 ? "border-t border-hairline/60" : "border-t border-hairline"}>
      <div
        className="row-hover flex items-center gap-3 px-4 py-2.5"
        style={{ paddingLeft: `${16 + depth * 18}px` }}
      >
        {/* 開閉は本物のボタンにする（div+onClick だとキーボードで到達できない）。
            トグル類はボタンの外に置く＝入れ子ボタンを作らない。 */}
        {hasDetail ? (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={detailId}
            onClick={() => setOpen((o) => !o)}
            className="focus-ring flex min-w-0 flex-1 items-center gap-3 rounded text-left"
          >
            {head}
          </button>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-3">{head}</div>
        )}

        <div className="flex shrink-0 items-center gap-3">
          {!open && digest !== null && digest !== "—" && (
            <span className="tnum max-w-[220px] truncate text-2xs text-muted">{digest}</span>
          )}
          {saved && error === null && (
            <span className="text-2xs font-medium text-accent-deep" role="status">
              保存しました
            </span>
          )}
          {setting.kind === "tri" && (
            <TriToggle
              value={(setting.current.value as TriValue) ?? "off"}
              disabled={busy || setting.readonly === true}
              onChange={(v) => saveQuiet(setting.path, v)}
            />
          )}
          {setting.kind === "bool" && (
            <Toggle
              label={setting.label}
              checked={setting.current.value === true}
              disabled={busy || setting.readonly === true}
              onChange={(v) => saveQuiet(setting.path, v)}
            />
          )}
          {setting.kind === "info" && <span className="text-2xs text-faint">表示のみ</span>}
        </div>
      </div>

      {open && (
        <div
          id={detailId}
          className="space-y-3 bg-canvas/60 px-4 pb-4 pt-1"
          style={{ paddingLeft: `${47 + depth * 18}px` }}
        >
          <p className="max-w-[62ch] text-xs leading-relaxed text-muted">{setting.desc}</p>

          {setting.fixedNote !== undefined && (
            <p className="max-w-[62ch] text-2xs leading-relaxed text-faint">
              ※ {setting.fixedNote}
            </p>
          )}

          {blocked && (
            <p className="max-w-[62ch] rounded-md bg-warn-soft px-2.5 py-1.5 text-2xs text-warn">
              ONにしても効きません。先に「{setting.blockedBy.join("」「")}」を有効にしてください。
            </p>
          )}

          {!["bool", "tri", "info"].includes(setting.kind) && (
            <div className="max-w-[520px]">
              <ValueEditor setting={setting} onSave={save} busy={busy} idNames={idNames} />
            </div>
          )}

          {error !== null && (
            <p className="rounded-md bg-danger-soft px-2.5 py-1.5 text-2xs text-danger">{error}</p>
          )}

          {(setting.children?.length ?? 0) > 0 && (
            <div className="-mx-4 overflow-hidden rounded-md border border-hairline bg-surface"
              style={{ marginLeft: `-${47 + depth * 18}px`, marginRight: "-16px" }}
            >
              {setting.children?.map((child, i) => (
                <div key={child.path} className={i === 0 ? "-mt-px" : ""}>
                  <SettingRow setting={child} onSave={onSave} depth={depth + 1} idNames={idNames} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
