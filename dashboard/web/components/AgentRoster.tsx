/**
 * エージェントの増減。
 *
 * 「まず1体で体験して、良ければ増やす」を画面だけでできるようにする。
 * 2体目以降は **別のBotアカウント**が要る（1つのトークンで複数の人格には
 * なれない）ので、そこを明示しないと必ず詰まる。
 */
import { useCallback, useEffect, useState } from "react";

import { Button, Card, Chip, ErrorNote } from "./ui.tsx";
import { api, RequestError } from "../lib/api.ts";

type Agent = { id: string; name: string; requireMention: boolean };
type Channel = { id: string; name: string; kind: string };
type PersonaFile = { fileName: string; title: string; summary: string; isTemplate: boolean };

const inputClass =
  "w-full rounded border border-hairline bg-surface px-2.5 py-1.5 text-xs focus-ring";

export function AgentRoster({
  agents,
  onChanged,
}: {
  agents: Agent[];
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [botName, setBotName] = useState("");
  const [channels, setChannels] = useState<Channel[]>([]);
  const [channelId, setChannelId] = useState("");
  const [personas, setPersonas] = useState<PersonaFile[]>([]);
  const [persona, setPersona] = useState("");

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

  useEffect(() => {
    if (!adding) return;
    void api
      .get<{ personas: PersonaFile[] }>("/setup/personas")
      .then((got) => {
        setPersonas(got.personas);
        const first = got.personas.find((p) => !p.isTemplate) ?? got.personas[0];
        if (first) setPersona(first.fileName);
      })
      .catch(() => undefined);
  }, [adding]);

  const verify = () =>
    guard(async () => {
      const got = await api.post<{ bot: { username: string } }>("/setup/platform/verify", { token });
      setBotName(got.bot.username);
      // トークンが通ったらチャンネル一覧を出す。
      // サーバーは初回設定で決まっているので、こちらからは指定しない
      const ch = await api.post<{ channels: Channel[] }>("/setup/platform/channels", {
        token,
      });
      setChannels(ch.channels.filter((c) => c.kind === "text"));
    });

  const add = () =>
    guard(async () => {
      await api.post("/setup/agents/add", {
        id,
        name,
        token,
        home_channel_id: channelId,
        persona_files: [`personas/${persona}`],
      });
      setAdding(false);
      setId("");
      setName("");
      setToken("");
      setBotName("");
      setChannelId("");
      onChanged();
    });

  const remove = (agentId: string, agentName: string) =>
    guard(async () => {
      if (!window.confirm(`${agentName} を削除します。よろしいですか？`)) return;
      await api.post("/setup/agents/remove", { id: agentId });
      onChanged();
    });

  return (
    <Card
      title="エージェントの増減"
      desc="1体で試して、良ければ増やせます。2体目以降は別のBotアカウントが必要です。"
    >
      <div className="space-y-3 px-4 py-3">
        {error !== null && <ErrorNote message={error} />}

        <ul className="space-y-1">
          {agents.map((a) => (
            <li
              key={a.id}
              className="flex items-center gap-2 rounded border border-hairline px-3 py-2"
            >
              <span className="text-xs font-medium">{a.name}</span>
              <span className="font-mono text-2xs text-faint">{a.id}</span>
              {a.requireMention && <Chip>呼ばれた時だけ</Chip>}
              <Button
                variant="danger"
                busy={busy}
                onClick={() => void remove(a.id, a.name)}
              >
                削除
              </Button>
            </li>
          ))}
        </ul>

        {adding ? (
          <div className="space-y-2 rounded border border-hairline px-3 py-3">
            <p className="text-2xs text-muted">
              Developer Portal でもう1つ Application を作り、その Bot のトークンを貼ってください
              （MESSAGE CONTENT INTENT の有効化もお忘れなく）。
            </p>
            <label className="block space-y-1">
              <span className="text-xs font-medium">Botトークン</span>
              <input
                className={inputClass}
                type="password"
                value={token}
                autoComplete="off"
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
            <div className="flex items-center gap-2">
              <Button busy={busy} disabled={token.trim() === ""} onClick={() => void verify()}>
                接続テスト
              </Button>
              {botName !== "" && (
                <span className="text-2xs text-accent">つながりました: {botName}</span>
              )}
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block space-y-1">
                <span className="text-xs font-medium">ID（英小文字）</span>
                <input
                  className={inputClass}
                  value={id}
                  onChange={(e) => setId(e.target.value)}
                  placeholder="agent2"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-xs font-medium">表示名</span>
                <input
                  className={inputClass}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="ひかり"
                />
              </label>
            </div>

            {channels.length > 0 && (
              <label className="block space-y-1">
                <span className="text-xs font-medium">ホームチャンネル</span>
                <select
                  className={inputClass}
                  value={channelId}
                  onChange={(e) => setChannelId(e.target.value)}
                >
                  <option value="">選んでください</option>
                  {channels.map((c) => (
                    <option key={c.id} value={c.id}>
                      #{c.name}
                    </option>
                  ))}
                </select>
              </label>
            )}

            <label className="block space-y-1">
              <span className="text-xs font-medium">性格</span>
              <select
                className={inputClass}
                value={persona}
                onChange={(e) => setPersona(e.target.value)}
              >
                {personas.map((p) => (
                  <option key={p.fileName} value={p.fileName}>
                    {p.title}
                    {p.isTemplate ? "（見本）" : ""}
                  </option>
                ))}
              </select>
            </label>

            <div className="flex gap-2">
              <Button
                variant="primary"
                busy={busy}
                disabled={id === "" || name === "" || token === "" || channelId === "" || persona === ""}
                onClick={() => void add()}
              >
                追加する
              </Button>
              <Button onClick={() => setAdding(false)}>やめる</Button>
            </div>
          </div>
        ) : (
          <Button onClick={() => setAdding(true)}>エージェントを追加</Button>
        )}
      </div>
    </Card>
  );
}
