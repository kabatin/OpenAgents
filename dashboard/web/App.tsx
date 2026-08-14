import { useCallback, useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";

import { Avatar } from "./components/Avatar.tsx";
import { PendingBar } from "./components/PendingBar.tsx";
import { useEventStream, useFetch } from "./lib/api.ts";
import { AgentPage } from "./pages/AgentPage.tsx";
import { DataPage } from "./pages/DataPage.tsx";
import { OpsPage } from "./pages/OpsPage.tsx";
import { OverviewPage } from "./pages/OverviewPage.tsx";
import { PersonaPage } from "./pages/PersonaPage.tsx";
import { SetupPage } from "./pages/SetupPage.tsx";
import { SettingsPage } from "./pages/SettingsPage.tsx";
import type {
  ActivityRow,
  AgentSummary,
  Overview,
  PendingView,
  QuotaRow,
  ServiceStatus,
} from "./lib/types.ts";

function Nav({
  services,
  agents,
  connected,
}: {
  services: ServiceStatus[];
  /** 設定に登録されているエージェント（固定の一覧は持たない） */
  agents: { id: string; label: string }[];
  connected: boolean;
}) {
  const location = useLocation();
  const archive = services.find((s) => s.id === "archivebot");
  const dev = services.find((s) => s.id === "devbot");
  const statusFor = (id: string) =>
    (id === "devbot" ? dev?.status : archive?.status) ?? "unknown";

  const link = (to: string, label: string, extra?: React.ReactNode) => {
    const active = location.pathname === to;
    return (
      <NavLink
        key={to}
        to={to}
        className={`focus-ring flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] transition-colors duration-100
          ${active ? "bg-ink text-white" : "text-muted hover:bg-canvas hover:text-ink"}`}
      >
        {extra}
        <span className={active ? "font-medium" : ""}>{label}</span>
      </NavLink>
    );
  };

  return (
    <header className="sticky top-0 z-20 border-b border-hairline bg-surface/90 backdrop-blur">
      <div className="mx-auto flex max-w-[1180px] items-center gap-1 px-6 py-2.5">
        <NavLink to="/" className="focus-ring mr-4 flex items-baseline gap-2 rounded">
          <span className="text-[15px] font-semibold tracking-tight">AIエージェント管理</span>
        </NavLink>

        {link("/", "概要")}
        <span className="mx-1.5 h-4 w-px bg-hairline" />
        {agents.map((a) =>
          link(
            `/agents/${a.id}`,
            a.label,
            <Avatar id={a.id} name={a.label} size="sm" status={statusFor(a.id)} />,
          ),
        )}
        <span className="mx-1.5 h-4 w-px bg-hairline" />
        {link("/personas", "性格")}
        {link("/settings", "全体設定")}
        {link("/ops", "運用")}
        {link("/data", "データ")}

        <span
          className="ml-auto flex items-center gap-1.5 text-2xs text-faint"
          title={connected ? "リアルタイム更新中" : "再接続しています"}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-accent" : "bg-faint"}`}
          />
          {connected ? "ライブ" : "接続待ち"}
        </span>
      </div>
    </header>
  );
}

/** ページを切り替えたら先頭へ戻す（SPAは既定でスクロール位置を持ち越してしまう）。 */
function ScrollToTop() {
  const { pathname } = useLocation();
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  return null;
}

type SetupState = { configExists: boolean; needsSetup: boolean; agentCount: number };

export default function App() {
  // まだ設定が無ければ、他の画面は見せずにセットアップへ連れていく。
  // 「空っぽの管理画面」を見せても、何をすればいいか分からない
  const { data: setupState, reload: reloadSetup } = useFetch<SetupState>("/setup/state");
  const { data: initial, reload } = useFetch<Overview>("/overview");
  const [services, setServices] = useState<ServiceStatus[]>([]);
  const [pending, setPending] = useState<PendingView[]>([]);
  const [quota, setQuota] = useState<QuotaRow[]>([]);
  const [activity, setActivity] = useState<ActivityRow[]>([]);

  const { connected } = useEventStream({
    status: (d) => setServices(d as ServiceStatus[]),
    pending: (d) => setPending(d as PendingView[]),
    quota: (d) => setQuota(d as QuotaRow[]),
    activity: (d) => setActivity((prev) => [...(d as ActivityRow[]), ...prev].slice(0, 120)),
  });

  const liveServices = services.length > 0 ? services : (initial?.services ?? []);
  const livePending = pending.length > 0 ? pending : (initial?.pending ?? []);

  // SSEで届いた最新の枠消化をサマリへ差し込む（画面全体を取り直さない）
  const agents: AgentSummary[] = (initial?.agents ?? []).map((a) => {
    const q = quota.find((x) => x.agentId === a.id);
    if (q === undefined) return a;
    return {
      ...a,
      quota: { ...a.quota, used: q.used, limit: q.dbOverride ?? a.quota.limit },
    };
  });

  const onChanged = useCallback(() => reload(), [reload]);
  const onSetupDone = useCallback(() => {
    reloadSetup();
    reload();
  }, [reloadSetup, reload]);

  // ナビに出すエージェントは設定から来る（固定の一覧は持たない）。
  // 開発BOTは agents[] に居ない別プロセスなので、有効なときだけ足す
  const navAgents = [
    ...agents.map((a) => ({ id: a.id, label: a.name })),
    ...(liveServices.some((s) => s.id === "devbot" && s.enabled)
      ? [{ id: "devbot", label: "開発BOT" }]
      : []),
  ];

  if (setupState?.needsSetup === true) {
    return (
      <div className="min-h-screen">
        <main className="mx-auto max-w-[1180px] px-6 pb-24 pt-10">
          <SetupPage onDone={onSetupDone} />
        </main>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <Nav services={liveServices} agents={navAgents} connected={connected} />
      <PendingBar pending={livePending} onApplied={onChanged} />
      <ScrollToTop />
      {/* 末尾に余白を置いて、最後のカードが画面の底に貼り付かないようにする */}
      <main className="mx-auto max-w-[1180px] px-6 pb-24 pt-7">
        <Routes>
          <Route
            path="/"
            element={
              <OverviewPage agents={agents} services={liveServices} activity={activity} />
            }
          />
          <Route path="/agents/:id" element={<AgentPage onChanged={onChanged} />} />
          <Route path="/settings" element={<SettingsPage onChanged={onChanged} />} />
          <Route path="/ops" element={<OpsPage services={liveServices} />} />
          <Route path="/data" element={<DataPage />} />
          <Route path="/personas" element={<PersonaPage />} />
          <Route path="/setup" element={<SetupPage onDone={onSetupDone} />} />
        </Routes>
      </main>
    </div>
  );
}
