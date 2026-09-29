import { useState, type ReactNode } from "react";

import { STATUS_TONE } from "../lib/format.ts";
import type { HealthStatus } from "../lib/types.ts";

export function StatusDot({
  status,
  pulse,
}: {
  status: HealthStatus;
  pulse?: boolean;
}) {
  const tone = STATUS_TONE[status];
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0">
      {pulse && status === "ok" && (
        <span
          className={`absolute inline-flex h-full w-full animate-ping rounded-full ${tone.dot} opacity-40`}
        />
      )}
      <span
        className={`relative inline-flex h-2 w-2 rounded-full ${tone.dot}`}
      />
    </span>
  );
}

export function Chip({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "accent" | "warn" | "danger" | "info" | "plum";
  children: ReactNode;
}) {
  const tones = {
    neutral: "bg-canvas text-muted border border-hairline",
    accent: "bg-accent-soft text-accent-deep",
    warn: "bg-warn-soft text-warn",
    danger: "bg-danger-soft text-danger",
    info: "bg-info-soft text-info",
    plum: "bg-plum-soft text-plum",
  } as const;
  return <span className={`chip whitespace-nowrap ${tones[tone]}`}>{children}</span>;
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`focus-ring relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full
        border transition-colors duration-150
        ${checked ? "border-accent bg-accent" : "border-hairline bg-[#DEDCD7]"}
        ${disabled ? "cursor-not-allowed opacity-40" : "cursor-pointer"}`}
    >
      <span
        className={`inline-block h-[16px] w-[16px] rounded-full bg-white shadow-sm transition-transform duration-150
          ${checked ? "translate-x-[19px]" : "translate-x-[3px]"}`}
      />
    </button>
  );
}

export type TriValue = "off" | "shadow" | "live";

/** OFF / シャドー / 本番 の3値。シャドーは「実行するが投稿しない」安全モード。 */
export function TriToggle({
  value,
  onChange,
  disabled,
}: {
  value: TriValue;
  onChange: (v: TriValue) => void;
  disabled?: boolean;
}) {
  const opts: { v: TriValue; label: string; on: string }[] = [
    { v: "off", label: "OFF", on: "bg-white text-ink shadow-sm" },
    { v: "shadow", label: "シャドー", on: "bg-warn text-white shadow-sm" },
    { v: "live", label: "本番", on: "bg-accent text-white shadow-sm" },
  ];
  return (
    <div
      role="radiogroup"
      className={`inline-flex shrink-0 rounded-md border border-hairline bg-[#F1EFEB] p-[2px] ${
        disabled ? "opacity-40" : ""
      }`}
    >
      {opts.map((o, i) => (
        <button
          key={o.v}
          type="button"
          role="radio"
          aria-checked={value === o.v}
          // ARIAのradioグループは「選択中だけがTab停止、左右キーで移動」が作法
          tabIndex={value === o.v ? 0 : -1}
          disabled={disabled}
          onClick={() => onChange(o.v)}
          onKeyDown={(e) => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            e.preventDefault();
            const next =
              opts[
                (i + (e.key === "ArrowRight" ? 1 : opts.length - 1)) %
                  opts.length
              ];
            if (next !== undefined) onChange(next.v);
          }}
          className={`focus-ring rounded-[5px] px-2.5 py-[3px] text-2xs font-semibold transition-all duration-150
            ${value === o.v ? o.on : "text-muted hover:text-ink"}
            ${disabled ? "cursor-not-allowed" : "cursor-pointer"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Card({
  title,
  eyebrow,
  desc,
  right,
  children,
  className = "",
}: {
  title?: string;
  eyebrow?: string;
  desc?: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title !== undefined || right !== undefined) && (
        <header className="flex items-start justify-between gap-4 border-b border-hairline px-4 py-3">
          <div className="min-w-0">
            {eyebrow !== undefined && (
              <div className="eyebrow mb-1">{eyebrow}</div>
            )}
            {title !== undefined && (
              <h2 className="text-[15px] font-semibold tracking-tight">
                {title}
              </h2>
            )}
            {desc !== undefined && (
              <p className="mt-1 text-xs leading-relaxed text-muted">{desc}</p>
            )}
          </div>
          {right !== undefined && <div className="shrink-0">{right}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Button({
  onClick,
  children,
  variant = "ghost",
  disabled,
  busy,
  type = "button",
}: {
  onClick?: () => void;
  children: ReactNode;
  variant?: "primary" | "ghost" | "danger";
  disabled?: boolean;
  busy?: boolean;
  type?: "button" | "submit";
}) {
  const variants = {
    primary: "bg-accent text-white hover:bg-accent-deep border-transparent",
    ghost: "bg-surface text-ink hover:bg-canvas border-hairline",
    danger: "bg-surface text-danger hover:bg-danger-soft border-hairline",
  } as const;
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled === true || busy === true}
      className={`focus-ring inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5
        text-xs font-semibold transition-colors duration-100 disabled:cursor-not-allowed
        disabled:opacity-50 ${variants[variant]}`}
    >
      {busy === true && (
        <span className="h-3 w-3 animate-spin rounded-full border-[1.5px] border-current border-t-transparent" />
      )}
      {children}
    </button>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="px-4 py-10 text-center text-xs text-muted">{children}</div>
  );
}

/**
 * 読み込み中のプレースホルダ。
 * 「まだ来ていない」と「0件だった」は別物なので、必ず描き分ける
 * （空表示で代用すると、画面が「ありません」と嘘をつく）。
 */
export function Loading({
  rows = 3,
  label = "読み込んでいます…",
}: {
  rows?: number;
  label?: string;
}) {
  return (
    <div className="px-4 py-3" role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      <div className="space-y-2">
        {Array.from({ length: rows }, (_, i) => (
          <div
            key={i}
            className="h-3 animate-pulse rounded bg-[#EDEBE7]"
            style={{ width: `${92 - i * 14}%` }}
          />
        ))}
      </div>
    </div>
  );
}

export function ErrorNote({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-xs text-danger">
      <span className="min-w-0 flex-1">{message}</span>
      {onRetry !== undefined && (
        <button
          type="button"
          onClick={onRetry}
          className="focus-ring shrink-0 rounded-md border border-danger/30 bg-surface px-2.5 py-1 text-2xs font-semibold text-danger hover:bg-danger-soft"
        >
          再試行
        </button>
      )}
    </div>
  );
}

/** useFetch の戻り値そのもの。Async に丸ごと渡せるようにしておく。 */
export type Query<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
};

/**
 * 「0件」の位置に置いて、読み込み中・失敗・本当に0件を描き分ける。
 * 既存の `{list.length === 0 ? <Empty/> : list.map(...)}` の <Empty> を
 * これに差し替えるだけで、画面が「ありません」と嘘をつかなくなる。
 */
export function ListState({ q, empty }: { q: Query<unknown>; empty: string }) {
  if (q.error !== null) {
    return (
      <div className="p-3">
        <ErrorNote message={q.error} onRetry={q.reload} />
      </div>
    );
  }
  if (q.loading || q.data === null) return <Loading />;
  return <Empty>{empty}</Empty>;
}

/**
 * 取得状態の描き分けを1箇所に集約する。
 * 読み込み中 → エラー（再試行つき）→ 空 → 本体 の順で判定する。
 */
export function Async<T>({
  q,
  empty,
  rows,
  children,
}: {
  q: Query<T>;
  empty?: ReactNode;
  rows?: number;
  children: (data: T) => ReactNode;
}) {
  if (q.error !== null)
    return (
      <div className="p-3">
        <ErrorNote message={q.error} onRetry={q.reload} />
      </div>
    );
  if (q.loading && q.data === null) return <Loading rows={rows} />;
  if (q.data === null) return <Empty>{empty ?? "データがありません"}</Empty>;
  if (Array.isArray(q.data) && q.data.length === 0)
    return <Empty>{empty ?? "データがありません"}</Empty>;
  return <>{children(q.data)}</>;
}

export function Metric({
  label,
  value,
  sub,
  tone = "ink",
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  tone?: "ink" | "accent" | "warn" | "danger";
}) {
  const tones = {
    ink: "text-ink",
    accent: "text-accent-deep",
    warn: "text-warn",
    danger: "text-danger",
  } as const;
  return (
    <div>
      <div className="eyebrow">{label}</div>
      <div
        className={`tnum mt-1 text-[22px] font-semibold leading-none tracking-tight ${tones[tone]}`}
      >
        {value}
      </div>
      {sub !== undefined && (
        <div className="mt-1 text-2xs text-faint">{sub}</div>
      )}
    </div>
  );
}

/**
 * ページの見出し＋「ここで何ができるか」。新人が開いた瞬間に触り始められるよう、
 * よく使う操作を tips に数行だけ添える。
 */
export function PageHeader({
  title,
  lead,
  tips,
  tipsKey,
}: {
  title: string;
  lead: ReactNode;
  tips?: ReactNode[];
  tipsKey?: string;
}) {
  return (
    <div className="space-y-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-1 max-w-[80ch] text-xs leading-relaxed text-muted">
          {lead}
        </p>
      </div>
      {tips !== undefined && tipsKey !== undefined && (
        <Tips tipsKey={tipsKey} tips={tips} />
      )}
    </div>
  );
}

/** 「はじめての方へ」の案内。閉じたら localStorage に記憶（読めない環境でも表示は壊さない）。 */
export function Tips({
  tipsKey,
  tips,
}: {
  tipsKey: string;
  tips: ReactNode[];
}) {
  const storageKey = `tips-closed:${tipsKey}`;
  const [closed, setClosed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(storageKey) === "1";
    } catch {
      return false;
    }
  });
  if (closed || tips.length === 0) return null;
  const close = () => {
    setClosed(true);
    try {
      window.localStorage.setItem(storageKey, "1");
    } catch {
      // 保存できない環境では、このページを開いている間だけ閉じる
    }
  };
  return (
    <div className="flex items-start gap-3 rounded-lg border border-info/20 bg-info-soft px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold text-info">はじめての方へ</div>
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs leading-relaxed text-ink">
          {tips.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ul>
      </div>
      <button
        type="button"
        onClick={close}
        className="focus-ring shrink-0 rounded px-1.5 text-2xs text-muted hover:text-ink"
        aria-label="案内を閉じる"
      >
        閉じる
      </button>
    </div>
  );
}

/** 下線タブ。ページ内の切り替えに使う（URLは変えない）。 */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: string; count?: number }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div
      className="flex flex-wrap gap-1 border-b border-hairline"
      role="tablist"
    >
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={`focus-ring -mb-px border-b-2 px-3 py-2 text-[13px] transition-colors duration-100 ${
            value === t.id
              ? "border-accent font-medium text-ink"
              : "border-transparent text-muted hover:text-ink"
          }`}
        >
          {t.label}
          {t.count !== undefined && (
            <span className="tnum ml-1.5 text-2xs text-faint">{t.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
