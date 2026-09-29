import { useState } from "react";

import type { ResolvedSetting } from "../lib/types.ts";
import { SettingRow, type IdNames, type SaveFn } from "./SettingRow.tsx";
import { Empty } from "./ui.tsx";

/**
 * 設定の一覧。「よく使う設定（level: basic）」を先に出し、残りは
 * 「詳細設定」として畳む。初めて開いた人が数十行の壁を見ないようにするため。
 * `showAll` のときは畳まずに全部出す（検索中・「すべての設定」タブ）。
 */
export function SettingsList({
  settings,
  onSave,
  idNames,
  showAll = false,
  empty = "設定はありません",
}: {
  settings: ResolvedSetting[];
  onSave: SaveFn;
  idNames: IdNames;
  showAll?: boolean;
  empty?: string;
}) {
  const [open, setOpen] = useState(false);
  const visible = settings.filter((s) => s.kind !== "info" || s.fixedNote !== undefined);
  if (visible.length === 0) return <Empty>{empty}</Empty>;

  const basic = visible.filter((s) => s.level === "basic");
  const rest = visible.filter((s) => s.level !== "basic");
  // よく使う設定が1つも無いまとまりは、畳むと空に見えるので全部出す
  if (showAll || basic.length === 0) {
    return (
      <div>
        {visible.map((s) => (
          <SettingRow key={s.path} setting={s} onSave={onSave} idNames={idNames} />
        ))}
      </div>
    );
  }

  return (
    <div>
      {basic.map((s) => (
        <SettingRow key={s.path} setting={s} onSave={onSave} idNames={idNames} />
      ))}
      {rest.length > 0 && (
        <div className="border-t border-hairline">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="focus-ring flex w-full items-center gap-2 px-4 py-2.5 text-left text-2xs text-muted hover:text-ink"
          >
            <span className={`transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
            {open ? "詳細設定を閉じる" : `詳細設定 ${rest.length}件（普段は触らなくて大丈夫です）`}
          </button>
          {open && (
            <div className="bg-canvas/40">
              {rest.map((s) => (
                <SettingRow key={s.path} setting={s} onSave={onSave} idNames={idNames} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
