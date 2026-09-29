import type { ReactNode } from "react";

/** 1パネルに出す最大件数。超えた分は件数だけ出す（黙って切らない）。 */
export const LIST_MAX = 60;

export function usd(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `$${n.toFixed(3)}`;
}

export function sec(ms: number | null | undefined): string {
  return ms === null || ms === undefined ? "—" : `${(ms / 1000).toFixed(1)}s`;
}

/** scope は `global` / `channel:<id>` / `user:<id>` の形で入っている。 */
export function scopeLabel(scope: string): string {
  if (scope === "global") return "全体";
  if (scope.startsWith("channel:")) return "チャンネル限定";
  if (scope.startsWith("user:")) return "個人限定";
  return scope;
}

/**
 * パネルの見出し。「これは何か」と「どう変えるか」を必ず添える
 * （データページは閲覧のみ。変え方を書かないと新人が手を止める）。
 */
export function PanelHead({
  title,
  what,
  how,
}: {
  title: string;
  what: ReactNode;
  how?: ReactNode;
}) {
  return (
    <header className="border-b border-hairline px-4 py-3">
      <h2 className="text-[15px] font-semibold tracking-tight">{title}</h2>
      <p className="mt-1 text-xs leading-relaxed text-muted">{what}</p>
      {how !== undefined && (
        <p className="mt-1 text-2xs leading-relaxed text-faint">
          <span className="font-semibold">変え方:</span> {how}
        </p>
      )}
    </header>
  );
}

/** パネル内の小見出し（1パネルに複数の一覧があるとき）。 */
export function SubHead({ children }: { children: ReactNode }) {
  return (
    <div className="eyebrow border-t border-hairline bg-canvas/60 px-4 py-2 first:border-t-0">
      {children}
    </div>
  );
}

/** 一覧の1行（行の見た目をデータページ全体でそろえる）。 */
export function Row({
  children,
  top = false,
}: {
  children: ReactNode;
  top?: boolean;
}) {
  return (
    <li
      className={`flex gap-2.5 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0 ${
        top ? "items-start" : "items-baseline"
      }`}
    >
      {children}
    </li>
  );
}

export function IdCell({ id }: { id: number | string }) {
  return <span className="tnum w-9 shrink-0 text-2xs text-faint">#{id}</span>;
}

export function MoreRow({ total, shown }: { total: number; shown: number }) {
  if (total <= shown) return null;
  return (
    <li className="border-t border-hairline px-4 py-2 text-2xs text-muted">
      他 {total - shown} 件（新しい順に{shown}件だけ表示しています）
    </li>
  );
}
