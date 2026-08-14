import { useState } from "react";

import { hourLabel, weekdayLabel } from "../lib/format.ts";
import type { ResolvedSetting } from "../lib/types.ts";
import { Button, Chip, Toggle, TriToggle, type TriValue } from "./ui.tsx";

export type SaveFn = (path: string, value: unknown) => Promise<void>;

/** 行の右端に出す「今どうなっているか」の短い要約。畳んだままでも状態が分かる。 */
function summarize(s: ResolvedSetting): string | null {
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
    case "intList":
      return Array.isArray(v) ? (v.length === 0 ? "未設定" : `${v.length}件`) : null;
    case "string":
    case "text":
      if (typeof v !== "string" || v.length === 0) return "未設定";
      return v.length > 22 ? `${v.slice(0, 22)}…` : v;
    default:
      return null;
  }
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
}: {
  setting: ResolvedSetting;
  onSave: SaveFn;
  busy: boolean;
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
    await onSave(setting.path, value);
    setDirty(false);
  };

  if (setting.readonly === true) {
    const v = setting.current.value;
    return (
      <span className="tnum text-xs text-muted">
        {Array.isArray(v) ? v.join(" / ") : v === null ? "—" : String(v)}
      </span>
    );
  }

  if (setting.kind === "enum") {
    return (
      <select
        className="input max-w-[220px]"
        disabled={busy}
        value={String(setting.current.value ?? "")}
        onChange={(e) => {
          const opt = setting.options?.find((o) => String(o.value ?? "") === e.target.value);
          void onSave(setting.path, opt?.value ?? null);
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
        onChange={(e) => void onSave(setting.path, Number(e.target.value))}
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
        onChange={(e) => void onSave(setting.path, Number(e.target.value))}
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
}: {
  setting: ResolvedSetting;
  onSave: SaveFn;
  depth?: number;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const blocked = setting.blockedBy.length > 0;
  const hasDetail =
    (setting.children?.length ?? 0) > 0 ||
    setting.fixedNote !== undefined ||
    blocked ||
    !["bool", "tri"].includes(setting.kind);

  const save: SaveFn = async (path, value) => {
    setBusy(true);
    setError(null);
    try {
      await onSave(path, value);
    } catch (e) {
      setError((e as Error).message);
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

  const digest = childDigest(setting) ?? summarize(setting);

  return (
    <div className={depth > 0 ? "border-t border-hairline/60" : "border-t border-hairline"}>
      <div
        className={`row-hover flex items-center gap-3 px-4 py-2.5 ${hasDetail ? "cursor-pointer" : ""}`}
        style={{ paddingLeft: `${16 + depth * 18}px` }}
        onClick={hasDetail ? () => setOpen((o) => !o) : undefined}
      >
        <span
          className={`w-3 shrink-0 text-2xs text-faint transition-transform duration-150 ${
            hasDetail ? "" : "opacity-0"
          } ${open ? "rotate-90" : ""}`}
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
            {setting.current.explicit === false && setting.kind !== "info" && (
              <Chip>既定値</Chip>
            )}
            {blocked && <Chip tone="warn">前提が未設定</Chip>}
          </div>
          {!open && digest !== null && digest !== "—" && (
            <div className="tnum mt-0.5 truncate text-2xs text-faint">{digest}</div>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-3" onClick={(e) => e.stopPropagation()}>
          {setting.kind === "tri" && (
            <TriToggle
              value={(setting.current.value as TriValue) ?? "off"}
              disabled={busy || setting.readonly === true}
              onChange={(v) => void save(setting.path, v)}
            />
          )}
          {setting.kind === "bool" && (
            <Toggle
              label={setting.label}
              checked={setting.current.value === true}
              disabled={busy || setting.readonly === true}
              onChange={(v) => void save(setting.path, v)}
            />
          )}
          {setting.kind === "info" && <span className="text-2xs text-faint">表示のみ</span>}
        </div>
      </div>

      {open && (
        <div
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
              <ValueEditor setting={setting} onSave={save} busy={busy} />
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
                  <SettingRow setting={child} onSave={onSave} depth={depth + 1} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
