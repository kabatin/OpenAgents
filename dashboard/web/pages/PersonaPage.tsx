/**
 * 性格（personas/）と前提知識（knowledge/）の編集。
 *
 * 「どう話すか」と「何を知っているか」は、エージェントの印象をいちばん
 * 大きく変える部分なのに、これまでは手でファイルを開くしかなかった。
 * ここで直せるようにする。
 *
 * 同梱のテンプレートは上書きさせない（元に戻せなくなるため）。
 * テンプレートを選んで「コピーして編集」すると自分用のファイルができる。
 */
import { useCallback, useEffect, useState } from "react";

import { Button, Card, Chip, Empty, ErrorNote, Toggle } from "../components/ui.tsx";
import { api, RequestError } from "../lib/api.ts";

type AgentRef = { id: string; name: string };

type PersonaFile = {
  relPath: string;
  fileName: string;
  isTemplate: boolean;
  title: string;
  summary: string;
  placeholders: string[];
  /** このファイルを読んでいるエージェント（空＝どこからも読まれていない） */
  usedBy: AgentRef[];
};

type Area = "personas" | "knowledge";

export function PersonaPage() {
  const [area, setArea] = useState<Area>("personas");
  const [files, setFiles] = useState<Record<Area, PersonaFile[]>>({
    personas: [],
    knowledge: [],
  });
  const [agents, setAgents] = useState<AgentRef[]>([]);
  const [selected, setSelected] = useState<PersonaFile | null>(null);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const guard = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof RequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const reload = useCallback(
    () =>
      guard(async () => {
        const got = await api.get<{
          personas: PersonaFile[];
          knowledge: PersonaFile[];
          agents: AgentRef[];
        }>("/setup/personas");
        setFiles({ personas: got.personas, knowledge: got.knowledge });
        setAgents(got.agents);
      }),
    [guard],
  );

  useEffect(() => {
    void reload();
  }, [reload]);

  const open = (file: PersonaFile) =>
    guard(async () => {
      const got = await api.get<{ content: string }>(
        `/setup/personas/${area}/${encodeURIComponent(file.fileName)}`,
      );
      setSelected(file);
      setContent(got.content);
      setDirty(false);
      setNotice("");
    });

  const save = () =>
    guard(async () => {
      if (selected === null) return;
      await api.post("/setup/personas/save", {
        area,
        fileName: selected.fileName,
        content,
      });
      setDirty(false);
      setNotice("保存しました。次の回答から反映されます（再起動は不要です）");
      await reload();
    });

  const copyToEdit = () =>
    guard(async () => {
      if (selected === null) return;
      // 名前はサーバーが決める（`company.example.md` → `company.md`）
      const got = await api.post<{ fileName: string; relPath: string }>(
        "/setup/personas/copy",
        { area, fileName: selected.fileName },
      );
      setNotice(
        `${got.fileName} を作りました。編集したら、下の「読ませる相手」で使う設定にしてください`,
      );
      await reload();
      setSelected({
        ...selected,
        fileName: got.fileName,
        relPath: got.relPath,
        isTemplate: false,
        usedBy: [],
      });
    });

  /** このファイルを読む/読まないを、エージェント単位で切り替える。 */
  const toggleUse = (agent: AgentRef, use: boolean) =>
    guard(async () => {
      if (selected === null) return;
      await api.post("/setup/personas/use", {
        agentId: agent.id,
        relPath: selected.relPath,
        use,
      });
      setNotice(
        use
          ? `${agent.name} が読むようにしました。適用（再起動）で反映されます`
          : `${agent.name} は読まなくなります。適用（再起動）で反映されます`,
      );
      await reload();
      setSelected({
        ...selected,
        usedBy: use
          ? [...selected.usedBy.filter((a) => a.id !== agent.id), agent]
          : selected.usedBy.filter((a) => a.id !== agent.id),
      });
    });

  const removeFile = () =>
    guard(async () => {
      if (selected === null) return;
      if (!window.confirm(`${selected.fileName} を削除します。元に戻せません。`)) return;
      await api.post("/setup/personas/delete", { area, fileName: selected.fileName });
      setNotice(`${selected.fileName} を削除しました`);
      setSelected(null);
      setContent("");
      await reload();
    });

  const list = files[area];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">性格と前提知識</h1>
        <p className="mt-1 text-xs text-muted">
          ここに書いた内容は、回答を作るたびにそのままAIへ渡されます。
          <b>短いほどよく効きます</b>（長い設定文は指示を薄めます）。
        </p>
      </div>

      {error !== null && <ErrorNote message={error} />}

      <div className="flex gap-1.5">
        {(["personas", "knowledge"] as const).map((a) => (
          <button
            key={a}
            type="button"
            onClick={() => {
              setArea(a);
              setSelected(null);
            }}
            className={`focus-ring rounded px-2.5 py-1 text-xs ${
              area === a ? "bg-accent text-white" : "text-muted hover:bg-hairline/50"
            }`}
          >
            {a === "personas" ? "性格（どう話すか）" : "前提知識（何を知っているか）"}
          </button>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        <Card title="ファイル">
          {list.length === 0 ? (
            <Empty>ファイルがありません</Empty>
          ) : (
            <ul>
              {list.map((f) => (
                <li key={f.fileName} className="border-t border-hairline first:border-t-0">
                  <button
                    type="button"
                    onClick={() => void open(f)}
                    className={`focus-ring block w-full px-4 py-2 text-left ${
                      selected?.fileName === f.fileName ? "bg-accent/5" : "hover:bg-hairline/30"
                    }`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-xs font-medium">{f.title}</span>
                      {f.isTemplate && <Chip>見本</Chip>}
                      {/* 読まれていないファイルは、書いても効かないので目印を出す */}
                      {!f.isTemplate &&
                        (f.usedBy.length > 0 ? (
                          <Chip tone="accent">使用中</Chip>
                        ) : (
                          <Chip tone="warn">未使用</Chip>
                        ))}
                    </span>
                    {f.summary !== "" && (
                      <span className="mt-0.5 block text-2xs text-muted">{f.summary}</span>
                    )}
                    <span className="mt-0.5 block font-mono text-2xs text-faint">
                      {f.fileName}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title={selected === null ? "選んでください" : selected.title}
          desc={
            selected?.isTemplate === true
              ? "同梱の見本です。そのままは編集できないので、コピーしてから直してください。"
              : undefined
          }
        >
          {selected === null ? (
            <Empty>左の一覧からファイルを選ぶと、ここで編集できます</Empty>
          ) : (
            <div className="space-y-3 px-4 py-3">
              {selected.placeholders.length > 0 && (
                <p className="text-2xs text-muted">
                  差し込み語: {selected.placeholders.map((p) => `{{${p}}}`).join(" / ")}
                </p>
              )}
              <textarea
                className="focus-ring h-[420px] w-full resize-y rounded border border-hairline bg-surface px-3 py-2 font-mono text-xs leading-relaxed"
                value={content}
                spellCheck={false}
                onChange={(e) => {
                  setContent(e.target.value);
                  setDirty(true);
                  setNotice("");
                }}
              />
              <div className="flex flex-wrap items-center gap-2">
                {selected.isTemplate ? (
                  <Button variant="primary" busy={busy} onClick={() => void copyToEdit()}>
                    コピーして編集
                  </Button>
                ) : (
                  <Button variant="primary" busy={busy} disabled={!dirty} onClick={() => void save()}>
                    保存
                  </Button>
                )}
                {!selected.isTemplate && (
                  <Button busy={busy} onClick={() => void removeFile()}>
                    削除
                  </Button>
                )}
                <span className="text-2xs text-muted">
                  {notice !== "" ? notice : dirty ? "未保存の変更があります" : ""}
                </span>
                <span className="ml-auto text-2xs text-faint">{content.length} 文字</span>
              </div>

              {/* 読ませる相手。ここで選ばないと、書いた内容は誰にも渡らない */}
              {!selected.isTemplate && (
                <div className="rounded border border-hairline">
                  <div className="border-b border-hairline px-3 py-2">
                    <div className="eyebrow">読ませる相手</div>
                    <p className="mt-1 text-2xs leading-relaxed text-muted">
                      オンにしたエージェントだけが、この内容を毎回受け取ります。
                      どこにもオンが無いと、書いても使われません。
                    </p>
                  </div>
                  {agents.length === 0 ? (
                    <Empty>エージェントがいません</Empty>
                  ) : (
                    <ul>
                      {agents.map((a) => {
                        const on = selected.usedBy.some((u) => u.id === a.id);
                        return (
                          <li
                            key={a.id}
                            className="flex items-center gap-3 border-t border-hairline px-3 py-2 text-xs first:border-t-0"
                          >
                            <span className="flex-1 truncate">{a.name}</span>
                            <span className="text-2xs text-faint">
                              {on ? "読ませる" : "読ませない"}
                            </span>
                            <Toggle
                              checked={on}
                              disabled={busy}
                              label={`${a.name} に ${selected.title} を読ませる`}
                              onChange={(v) => void toggleUse(a, v)}
                            />
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
