import { useEffect, useState } from "react";

import { api } from "../lib/api.ts";
import type { PendingView, RestartResult } from "../lib/types.ts";
import { Button } from "./ui.tsx";

/** 生のJSON値を人間向けの言葉にする（`true` / `null` / `[]` をそのまま見せない）。 */
function valueText(v: unknown): string {
  if (v === null || v === undefined) return "未設定";
  if (typeof v === "boolean") return v ? "ON" : "OFF";
  if (Array.isArray(v)) return v.length === 0 ? "空" : `${v.length}件`;
  if (typeof v === "object") return "（複数の項目）";
  const s = String(v);
  if (s === "off") return "OFF";
  if (s === "shadow") return "シャドー";
  if (s === "live") return "本番";
  return s === "" ? "未設定" : s;
}

/**
 * 「保存したがBOTにはまだ反映されていない変更」のバー。
 *
 * config.json は起動時にしか読まれないので、保存＝反映ではない。
 * この乖離を隠すと画面が嘘をつくため、常に目に入る位置に出す。
 *
 * 適用＝BOTの再起動なので、運用ページの再起動と同じく2段階で確認する
 * （このバーは常時最上部に出るぶん、誤クリックの危険が一番高い）。
 */
export function PendingBar({ pending, onApplied }: { pending: PendingView[]; onApplied: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  // 結果表示は出しっぱなしにしない（次の操作の結果と区別が付かなくなる）
  useEffect(() => {
    if (result === null || !result.ok) return;
    const t = setTimeout(() => setResult(null), 8000);
    return () => clearTimeout(t);
  }, [result]);

  // Esc で確認を取り消せるようにする（押してしまった時の逃げ道）
  useEffect(() => {
    if (confirming === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setConfirming(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming]);

  const live = pending.filter((p) => p.count > 0);
  if (live.length === 0) return null;

  const total = live.reduce((n, p) => n + p.count, 0);

  const apply = async (owner: string, label: string) => {
    setBusy(owner);
    setResult(null);
    try {
      const r = await api.post<RestartResult>(`/apply/${owner}`);
      setResult({ ok: r.ok, text: r.ok ? `${label} を再起動しました（${r.detail}）` : r.detail });
      if (r.ok) {
        setOpen(false);
        onApplied();
      }
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  };

  // 再起動中は他のボタンも止める（複数プロセスを立て続けに落とさない）
  const locked = busy !== null;

  return (
    <div className="border-b border-warn/25 bg-warn-soft/95 backdrop-blur">
      <div className="mx-auto max-w-[1180px] px-6">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 py-2.5">
          <span className="flex items-center gap-2 text-xs font-semibold text-warn">
            <span className="inline-flex h-1.5 w-1.5 rounded-full bg-warn" />
            未適用の変更 {total}件
          </span>
          <span className="text-2xs text-warn/80">
            設定は保存済みですが、BOTを再起動するまで反映されません
          </span>
          <div className="ml-auto flex items-center gap-2">
            <Button disabled={locked} onClick={() => setOpen((o) => !o)}>
              {open ? "差分を隠す" : "差分を見る"}
            </Button>
            {live.map((p) =>
              confirming === p.owner ? (
                <span key={p.owner} className="flex items-center gap-1.5">
                  <Button
                    variant="danger"
                    busy={busy === p.owner}
                    onClick={() => void apply(p.owner, p.label)}
                  >
                    本当に再起動
                  </Button>
                  <Button disabled={locked} onClick={() => setConfirming(null)}>
                    やめる
                  </Button>
                </span>
              ) : (
                <Button
                  key={p.owner}
                  variant="primary"
                  disabled={locked}
                  onClick={() => setConfirming(p.owner)}
                >
                  適用（{p.label} を再起動）
                </Button>
              ),
            )}
          </div>
        </div>

        {confirming !== null && (
          <div className="mb-2.5 rounded-md bg-surface px-3 py-2 text-2xs text-muted">
            再起動すると、そのプロセスのBOTは数十秒のあいだ応答しません。Esc で取り消せます。
          </div>
        )}

        {result !== null && (
          <div
            className={`mb-2.5 rounded-md px-3 py-2 text-xs ${
              result.ok ? "bg-accent-soft text-accent-deep" : "bg-danger-soft text-danger"
            }`}
            role="status"
          >
            {result.text}
          </div>
        )}

        {open && (
          <div className="mb-3 overflow-hidden rounded-md border border-warn/25 bg-surface">
            {live.map((p) => (
              <div key={p.owner}>
                <div className="border-b border-hairline bg-canvas px-3 py-1.5 text-2xs font-semibold text-muted">
                  {p.label} — 再起動が必要な変更 {p.count}件
                </div>
                {p.changes.map((c) => (
                  <div
                    key={c.path}
                    className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-hairline px-3 py-2 text-xs last:border-b-0"
                  >
                    {/* 生のconfigパスは見出しの下に小さく置く（主役は日本語のラベル） */}
                    <span className="min-w-0">
                      <span className="font-medium">{c.label}</span>
                      <span className="ml-2 font-mono text-2xs text-faint">{c.path}</span>
                    </span>
                    <span className="tnum ml-auto flex items-center gap-2">
                      <span className="rounded bg-danger-soft px-1.5 py-0.5 text-2xs text-danger line-through">
                        {valueText(c.before)}
                      </span>
                      <span className="text-muted">→</span>
                      <span className="rounded bg-accent-soft px-1.5 py-0.5 text-2xs text-accent-deep">
                        {valueText(c.after)}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
