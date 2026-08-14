import { useState } from "react";

import { STATUS_TONE } from "../lib/format.ts";
import type { HealthStatus } from "../lib/types.ts";

/** アイコンが取れない場合の背景色。エージェントごとに固定で、毎回同じ色になる。 */
const FALLBACK_TONE: Record<string, string> = {
  agent1: "bg-accent text-white",
  design: "bg-plum text-white",
  marketing: "bg-info text-white",
  devbot: "bg-warn text-white",
};

/** 「エージェント1」→「戦」。AIプレフィックスを落として1文字だけ拾う。 */
function initial(name: string): string {
  const stripped = name.replace(/^AI/i, "").trim();
  return (stripped[0] ?? name[0] ?? "?").toUpperCase();
}

const SIZES = {
  sm: { box: "h-6 w-6", text: "text-[11px]", ring: "ring-1" },
  md: { box: "h-9 w-9", text: "text-sm", ring: "ring-1" },
  lg: { box: "h-14 w-14", text: "text-xl", ring: "ring-2" },
} as const;

/**
 * エージェントのアイコン。Discordの実アイコンをサーバー経由で表示し、
 * 取れなければ頭文字にフォールバックする（画像が無くても崩れない）。
 */
export function Avatar({
  id,
  name,
  size = "md",
  status,
}: {
  id: string;
  name: string;
  size?: keyof typeof SIZES;
  status?: HealthStatus;
}) {
  const [failed, setFailed] = useState(false);
  const s = SIZES[size];

  return (
    <span className="relative inline-flex shrink-0">
      {failed ? (
        <span
          aria-hidden
          className={`${s.box} ${s.text} ${FALLBACK_TONE[id] ?? "bg-faint text-white"}
            flex items-center justify-center rounded-full font-semibold tracking-tight`}
        >
          {initial(name)}
        </span>
      ) : (
        <img
          src={`/api/avatars/${id}`}
          alt=""
          aria-hidden
          onError={() => setFailed(true)}
          className={`${s.box} rounded-full bg-canvas object-cover ${s.ring} ring-hairline`}
        />
      )}
      {status !== undefined && (
        <span
          className={`absolute -bottom-0.5 -right-0.5 rounded-full border-2 border-surface
            ${size === "lg" ? "h-3.5 w-3.5" : "h-2.5 w-2.5"} ${STATUS_TONE[status].dot}`}
          title={status}
        />
      )}
    </span>
  );
}
