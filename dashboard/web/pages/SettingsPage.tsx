import { useCallback, useState } from "react";

import { AgentRoster } from "../components/AgentRoster.tsx";
import { SettingRow, type SaveFn } from "../components/SettingRow.tsx";
import { Button, Card, Chip, Empty, ErrorNote } from "../components/ui.tsx";
import { api, useFetch } from "../lib/api.ts";
import type { SettingsView } from "../lib/types.ts";

/**
 * 初期化（危険な操作）。
 *
 * 軽い方と重い方を分けて並べる。ひとつのボタンに confirm を足すだけだと、
 * 「設定を入れ直したいだけ」の人が会話の記録まで消してしまう。
 * 逆に軽い方しか無いと、別サーバーへ繋ぎ替えたときに前のサーバーの会話が
 * 残り続ける（記録に guild_id が無いので、後から選んで消せない）。
 */
function DangerZone() {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState<"config" | "all" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState("");

  const run = async (scope: "config" | "all") => {
    const label = scope === "all" ? "会話の記録も含めて全部" : "設定だけ";
    if (!window.confirm(`${label}初期化します。よろしいですか？`)) return;
    setBusy(scope);
    setError(null);
    try {
      const got = await api.post<{ moved: string[] }>("/setup/reset", {
        scope,
        confirm: typed,
      });
      setDone(
        `初期化しました（${got.moved.length}件を退避）。` +
          "ブラウザを再読み込みすると、はじめの設定に戻ります。",
      );
      setTyped("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card
      title="危険な操作"
      desc="BOTを止めてから実行します。消したものは削除せず、日時つきの名前で退避します。"
      className="border-danger/30"
    >
      <div className="space-y-4 px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-xs font-medium">設定をやり直す</div>
            <p className="mt-0.5 text-2xs leading-relaxed text-muted">
              <code>config.json</code> を退避して、はじめの設定画面に戻します。
              <b>会話の記録・人格・前提知識は残ります。</b>
            </p>
          </div>
          <Button busy={busy === "config"} onClick={() => void run("config")}>
            やり直す
          </Button>
        </div>

        <div className="border-t border-hairline pt-4">
          <div className="text-xs font-medium text-danger">全部消して最初から</div>
          <p className="mt-0.5 max-w-[70ch] text-2xs leading-relaxed text-muted">
            設定に加えて <code>state/</code>（会話の記録・リマインダー・ログ）も
            初期状態に戻します。
            <b>別のDiscordサーバーで使い直すときは、必ずこちらを使ってください。</b>
            会話の記録にはサーバーの区別が無いため、設定だけ変えると前のサーバーの
            会話が残り、エージェントがそれを引用します。
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              className="focus-ring w-40 rounded border border-hairline bg-surface px-2 py-1 text-xs"
              placeholder="初期化 と入力"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            <Button
              variant="danger"
              busy={busy === "all"}
              disabled={typed.trim() !== "初期化"}
              onClick={() => void run("all")}
            >
              全部消す
            </Button>
          </div>
        </div>

        {error !== null && <ErrorNote message={error} />}
        {done !== "" && <p className="text-2xs text-accent-deep">{done}</p>}
      </div>
    </Card>
  );
}

/** 議事録の話者名マッピング。新メンバーが入ったらここで追加する。 */
function UserMapping({
  mapping,
  onSaved,
}: {
  mapping: Record<string, string>;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(mapping);
  const [newName, setNewName] = useState("");
  const [newMention, setNewMention] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = JSON.stringify(draft) !== JSON.stringify(mapping);

  const commit = async (next: Record<string, string>) => {
    setBusy(true);
    setError(null);
    try {
      await api.patch("/settings/meeting-users", { mapping: next });
      setDraft(next);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="p-4">
      <div className="overflow-hidden rounded-md border border-hairline">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-hairline bg-canvas">
              <th className="px-3 py-2 text-left font-semibold text-muted">Discordのユーザー名</th>
              <th className="px-3 py-2 text-left font-semibold text-muted">議事録での表記</th>
              <th className="w-10" />
            </tr>
          </thead>
          <tbody>
            {Object.entries(draft).map(([name, mention]) => (
              <tr key={name} className="border-b border-hairline last:border-b-0">
                <td className="px-3 py-1.5 font-mono text-2xs">{name}</td>
                <td className="px-3 py-1.5">
                  <input
                    className="input py-1 text-2xs"
                    value={mention}
                    disabled={busy}
                    onChange={(e) => setDraft({ ...draft, [name]: e.target.value })}
                  />
                </td>
                <td className="px-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      const next = { ...draft };
                      delete next[name];
                      setDraft(next);
                    }}
                    className="focus-ring rounded px-1.5 py-0.5 text-2xs text-faint hover:text-danger"
                  >
                    削除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <input
          className="input max-w-[200px] py-1 text-2xs"
          placeholder="新しいユーザー名"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <input
          className="input max-w-[220px] py-1 text-2xs"
          placeholder="<@123456789>"
          value={newMention}
          onChange={(e) => setNewMention(e.target.value)}
        />
        <Button
          disabled={newName.trim() === "" || newMention.trim() === ""}
          onClick={() => {
            setDraft({ ...draft, [newName.trim()]: newMention.trim() });
            setNewName("");
            setNewMention("");
          }}
        >
          追加
        </Button>
        <span className="ml-auto flex items-center gap-2">
          {dirty && <Chip tone="warn">未保存</Chip>}
          <Button variant="primary" disabled={!dirty} busy={busy} onClick={() => void commit(draft)}>
            マッピングを保存
          </Button>
        </span>
      </div>
      {error !== null && (
        <p className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-2xs text-danger">{error}</p>
      )}
    </div>
  );
}

export function SettingsPage({ onChanged }: { onChanged: () => void }) {
  const { data, error, reload } = useFetch<SettingsView>("/settings");
  const [saveError, setSaveError] = useState<string | null>(null);

  const saveFor = useCallback(
    (scope: string): SaveFn =>
      async (path, value) => {
        setSaveError(null);
        try {
          await api.patch("/config", { scope, changes: [{ path, value }] });
          reload();
          onChanged();
        } catch (e) {
          setSaveError((e as Error).message);
          throw e;
        }
      },
    [reload, onChanged],
  );

  if (error !== null) return <ErrorNote message={error} />;
  if (data === null) return <Empty>読み込んでいます…</Empty>;

  const saveGlobal = saveFor("global");
  const saveMeeting = saveFor("meeting");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">全体設定</h1>
        <p className="mt-1 max-w-[70ch] text-xs leading-relaxed text-muted">
          全エージェント共通の設定です。トークンは保存できますが、保存後は読み出せません
          （画面には ●●●● と表示されます）。
        </p>
      </div>

      {saveError !== null && <ErrorNote message={saveError} />}

      <AgentRoster agents={data.agents} onChanged={onChanged} />

      {data.global.map((g) => (
        <Card key={g.id} title={g.label} desc={g.desc}>
          {g.settings.map((s) => (
            <SettingRow key={s.path} setting={s} onSave={saveGlobal} />
          ))}
        </Card>
      ))}

      {data.devBot.map((g) => (
        <Card key={g.id} title={g.label} desc={g.desc}>
          {g.settings.map((s) => (
            <SettingRow key={s.path} setting={s} onSave={saveGlobal} />
          ))}
        </Card>
      ))}

      {data.meetingBot.map((g) => (
        <Card key={g.id} title={g.label} desc={g.desc}>
          {g.settings
            .filter((s) => s.kind !== "info")
            .map((s) => (
              <SettingRow key={s.path} setting={s} onSave={saveMeeting} />
            ))}
          <div className="border-t border-hairline">
            <div className="px-4 pt-3">
              <div className="eyebrow">話者名のマッピング（{Object.keys(data.meetingUserMapping).length}人）</div>
              <p className="mt-1 text-xs text-muted">
                録音した声を誰の発言として議事録に書くかの対応表です。
              </p>
            </div>
            <UserMapping
              mapping={data.meetingUserMapping}
              onSaved={() => {
                reload();
                onChanged();
              }}
            />
          </div>
        </Card>
      ))}

      <Card title="秘密情報" desc="config.json に平文で保存されています。編集はエディタで行ってください。">
        <ul>
          {Object.entries(data.secrets).map(([key, masked]) => (
            <li
              key={key}
              className="flex items-center justify-between gap-3 border-t border-hairline px-4 py-2.5 text-xs first:border-t-0"
            >
              <span className="font-mono text-2xs text-muted">{key}</span>
              <span className="tnum text-muted">{masked}</span>
            </li>
          ))}
        </ul>
      </Card>

      <DangerZone />
    </div>
  );
}
