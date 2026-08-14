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

import { Button, Card, Chip, Empty, ErrorNote } from "../components/ui.tsx";
import { api, RequestError } from "../lib/api.ts";

type PersonaFile = {
  relPath: string;
  fileName: string;
  isTemplate: boolean;
  title: string;
  summary: string;
  placeholders: string[];
};

type Area = "personas" | "knowledge";

export function PersonaPage() {
  const [area, setArea] = useState<Area>("personas");
  const [files, setFiles] = useState<Record<Area, PersonaFile[]>>({
    personas: [],
    knowledge: [],
  });
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
        const got = await api.get<{ personas: PersonaFile[]; knowledge: PersonaFile[] }>(
          "/setup/personas",
        );
        setFiles({ personas: got.personas, knowledge: got.knowledge });
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
      const base = selected.fileName.replace(/\.(template|example)\.md$/, "");
      const newName = `${base}-copy.md`;
      await api.post("/setup/personas/save", { area, fileName: newName, content });
      setNotice(`${newName} を作りました。こちらを編集してください`);
      await reload();
      setSelected({ ...selected, fileName: newName, isTemplate: false });
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
                <span className="text-2xs text-muted">
                  {notice !== "" ? notice : dirty ? "未保存の変更があります" : ""}
                </span>
                <span className="ml-auto text-2xs text-faint">{content.length} 文字</span>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
