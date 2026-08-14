import type { ReactNode } from "react";

import { STATUS_TONE } from "../lib/format.ts";
import type { HealthStatus } from "../lib/types.ts";

export function StatusDot({ status, pulse }: { status: HealthStatus; pulse?: boolean }) {
  const tone = STATUS_TONE[status];
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0">
      {pulse && status === "ok" && (
        <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${tone.dot} opacity-40`} />
      )}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${tone.dot}`} />
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
  return <span className={`chip ${tones[tone]}`}>{children}</span>;
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
      {opts.map((o) => (
        <button
          key={o.v}
          type="button"
          role="radio"
          aria-checked={value === o.v}
          disabled={disabled}
          onClick={() => onChange(o.v)}
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
            {eyebrow !== undefined && <div className="eyebrow mb-1">{eyebrow}</div>}
            {title !== undefined && (
              <h2 className="text-[15px] font-semibold tracking-tight">{title}</h2>
            )}
            {desc !== undefined && <p className="mt-1 text-xs leading-relaxed text-muted">{desc}</p>}
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
  return <div className="px-4 py-10 text-center text-xs text-faint">{children}</div>;
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-xs text-danger">
      {message}
    </div>
  );
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
      <div className={`tnum mt-1 text-[22px] font-semibold leading-none tracking-tight ${tones[tone]}`}>
        {value}
      </div>
      {sub !== undefined && <div className="mt-1 text-2xs text-faint">{sub}</div>}
    </div>
  );
}
