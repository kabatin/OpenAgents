/**
 * 初期セットアップのウィザード。
 *
 * 設定ファイルを一度も開かずに、Discordでエージェントが挨拶するところまで
 * 連れていくのがここの仕事。設計の要点は2つ:
 *
 *  1. **IDを手で調べさせない。** トークンさえ通れば、サーバーもチャンネルも
 *     一覧から選ばせる。開発者モードでIDをコピーさせる手順が最大の脱落ポイント
 *  2. **各ステップの成功を目に見せる。** Bot名が出る、AIが一言返す、
 *     Discordに挨拶が届く。「たぶん設定できた」で終わらせない
 */
import { useCallback, useEffect, useState } from "react";

import { Button, Card, ErrorNote } from "../components/ui.tsx";
import { api, RequestError } from "../lib/api.ts";

type BotIdentity = { id: string; username: string; avatarUrl: string };
type Guild = { id: string; name: string; iconUrl: string };
type Channel = { id: string; name: string; kind: string; parentId: string | null };
type Provider = {
  name: string;
  label: string;
  installed: boolean;
  path: string;
  install: string;
  rich: boolean;
  defaultModel: string;
};
type PersonaFile = {
  relPath: string;
  fileName: string;
  isTemplate: boolean;
  title: string;
  summary: string;
  placeholders: string[];
};

const STEPS = [
  "Discordの準備",
  "Botを作る",
  "居場所を決める",
  "使うAIを選ぶ",
  "性格を決める",
  "動かす",
];

function StepBar({ current }: { current: number }) {
  return (
    <ol className="mb-6 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs">
      {STEPS.map((label, i) => {
        const done = i < current;
        const now = i === current;
        return (
          <li key={label} className="flex items-center gap-2">
            <span
              className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-medium ${
                done
                  ? "bg-accent/20 text-accent"
                  : now
                    ? "bg-accent text-white"
                    : "bg-hairline text-faint"
              }`}
            >
              {done ? "✓" : i + 1}
            </span>
            <span className={now ? "font-medium" : "text-faint"}>{label}</span>
            {i < STEPS.length - 1 && <span className="text-faint">›</span>}
          </li>
        );
      })}
    </ol>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium">{label}</span>
      {children}
      {hint !== undefined && <span className="block text-2xs text-faint">{hint}</span>}
    </label>
  );
}

const inputClass =
  "w-full rounded border border-hairline bg-surface px-2.5 py-1.5 text-xs focus-ring";

export function SetupPage({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // ステップ2: Bot
  const [token, setToken] = useState("");
  const [bot, setBot] = useState<BotIdentity | null>(null);
  const [inviteUrl, setInviteUrl] = useState("");
  const [intentWarning, setIntentWarning] = useState("");

  // ステップ3: 居場所
  const [guilds, setGuilds] = useState<Guild[]>([]);
  const [guildId, setGuildId] = useState("");
  const [channels, setChannels] = useState<Channel[]>([]);
  const [channelId, setChannelId] = useState("");

  // ステップ4: AI
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("");
  const [llmReply, setLlmReply] = useState("");
  const [llmLimits, setLlmLimits] = useState("");

  // ステップ5: 性格
  const [templates, setTemplates] = useState<PersonaFile[]>([]);
  const [template, setTemplate] = useState("");
  const [agentName, setAgentName] = useState("");
  const [teamName, setTeamName] = useState("");

  // ステップ6: 完了
  const [jumpUrl, setJumpUrl] = useState("");
  const [launchWarning, setLaunchWarning] = useState("");

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
    // 設定ファイルの雛形を作る。失敗（権限・ディスク）を黙って飲むと、
    // 利用者は後のステップで意味の分からないエラーに出会うことになる
    api.post("/setup/init").catch((e) => {
      setError(
        `設定ファイルを用意できませんでした: ${
          e instanceof RequestError ? e.message : String(e)
        }。フォルダの書き込み権限を確認して、ページを再読み込みしてください`,
      );
    });
  }, []);

  // --- 各ステップの処理 ---

  const verifyToken = () =>
    guard(async () => {
      const got = await api.post<{
        bot: BotIdentity;
        inviteUrl: string;
        messageContentIntent: { enabled: boolean | null; detail: string };
      }>("/setup/platform/verify", { token });
      setBot(got.bot);
      setInviteUrl(got.inviteUrl);
      setIntentWarning(got.messageContentIntent.enabled === false ? got.messageContentIntent.detail : "");
    });

  /** チャンネル一覧を取り直す（guard の外側。呼ぶ側が包む）。 */
  const fetchChannels = async (gid: string) => {
    setGuildId(gid);
    // 前のサーバーのチャンネルを見せたまま取得を待たない
    // （取得に失敗すると、別サーバーのチャンネルIDが選ばれたままになる）
    setChannels([]);
    setChannelId("");
    const got = await api.post<{ channels: Channel[] }>("/setup/platform/channels", {
      token,
      guildId: gid,
    });
    setChannels(got.channels.filter((c) => c.kind === "text"));
  };

  const loadGuilds = () =>
    guard(async () => {
      const got = await api.post<{ guilds: Guild[] }>("/setup/platform/guilds", { token });
      setGuilds(got.guilds);
      setStep(2);
      // サーバーが1つなら選んだことにする。このとき**チャンネルの取得も
      // 一緒に走らせる**こと — 見た目は選択済みなのに次へ進めない、という
      // 手詰まりになる（選択欄の onChange は自動選択では発火しないため）
      if (got.guilds.length === 1 && got.guilds[0]) {
        await fetchChannels(got.guilds[0].id);
      }
    });

  const loadChannels = (gid: string) => guard(() => fetchChannels(gid));

  const detectLlm = () =>
    guard(async () => {
      const got = await api.post<{ providers: Provider[]; selected: string }>("/setup/llm/detect");
      setProviders(got.providers);
      const installed = got.providers.filter((p) => p.installed);
      const first = installed.find((p) => p.name === got.selected) ?? installed[0];
      if (first) {
        setProvider(first.name);
        setModel(first.defaultModel);
      }
      setStep(3);
    });

  const testLlm = () =>
    guard(async () => {
      const got = await api.post<{ reply: string; limits: string }>("/setup/llm/test", {
        provider,
        model,
      });
      setLlmReply(got.reply);
      setLlmLimits(got.limits);
    });

  const loadTemplates = () =>
    guard(async () => {
      const got = await api.get<{ personas: PersonaFile[] }>("/setup/personas");
      const tpl = got.personas.filter((p) => p.isTemplate);
      setTemplates(tpl);
      if (tpl[0]) setTemplate(tpl[0].fileName);
      setStep(4);
    });

  const finish = () =>
    guard(async () => {
      // 1) サーバーと使うAIを保存
      await api.post("/setup/platform/save", { guildId });
      await api.post("/setup/llm/save", { provider, model });

      // 2) 性格ファイルをテンプレートから作る
      const agentId = "agent1";
      const personaFile = `${agentId}.md`;
      await api.post("/setup/personas/create", {
        area: "personas",
        template,
        fileName: personaFile,
        values: { AGENT_NAME: agentName, TEAM_NAME: teamName || "このチーム" },
      });

      // 3) エージェントを登録。
      // 前回の試行が「登録後・挨拶前」で失敗していた場合、同じIDが既に
      // 居るのは**再試行の正常な形**なので、そこでは止めない
      // （それ以外の失敗は普通にエラーとして見せる）
      try {
        await api.post("/setup/agents/add", {
          id: agentId,
          name: agentName,
          token,
          home_channel_id: channelId,
          persona_files: [`personas/${personaFile}`],
        });
      } catch (e) {
        const message = e instanceof RequestError ? e.message : String(e);
        if (!message.includes("既に使われています")) throw e;
      }

      // 4) 挨拶を投稿して「本当に動いた」ところまで見せる
      const hello = await api.post<{ jumpUrl: string }>("/setup/hello", { agentId });
      setJumpUrl(hello.jumpUrl);

      // 5) BOT本体を起動する。挨拶はこの画面のサーバーが代理投稿しただけなので、
      //    ここを忘れると「挨拶は来たのに話しかけても無反応」になる。
      //    起動に失敗しても完了画面には進む（設定は全部保存済み）。
      //    ただし黙らず、理由と手動での起動方法を見せる
      try {
        await api.post("/setup/launch");
        setLaunchWarning("");
      } catch (e) {
        setLaunchWarning(e instanceof RequestError ? e.message : String(e));
      }
      setStep(5);
    });

  const canFinish = agentName.trim() !== "" && template !== "" && channelId !== "";

  return (
    <div className="mx-auto max-w-[720px] space-y-5">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">はじめの設定</h1>
        <p className="mt-1 text-xs text-muted">
          設定ファイルを開く必要はありません。この画面だけで、Discordにエージェントが
          住みつくところまで進みます。
        </p>
      </div>

      <StepBar current={step} />
      {error !== null && <ErrorNote message={error} />}

      {/* ── 1. Discordの準備 ───────────────────────── */}
      {step === 0 && (
        <Card title="1. Discord側の準備" desc="まず、エージェントの本体となる「Bot」を作ります。">
          <div className="space-y-3 px-4 py-3 text-xs leading-relaxed">
            <p>次の手順で進めてください（別タブで開くと楽です）。</p>
            <ol className="ml-4 list-decimal space-y-2">
              <li>
                <a
                  className="text-accent underline"
                  href="https://discord.com/developers/applications"
                  target="_blank"
                  rel="noreferrer"
                >
                  Discord Developer Portal
                </a>
                を開き、右上の <b>New Application</b> で新しいアプリを作る
              </li>
              <li>
                左メニューの <b>Bot</b> を開く
              </li>
              <li>
                <b className="text-accent">Privileged Gateway Intents</b> の
                <b> MESSAGE CONTENT INTENT</b> を<b>オンにして保存する</b>
                <div className="mt-1 rounded bg-hairline/40 px-2 py-1.5 text-2xs">
                  ⚠️ ここを飛ばすと、接続はできるのに<b>発言の中身が届かず、何をしても
                  無反応</b>になります。いちばん多いつまずきです。
                </div>
              </li>
              <li>
                同じ画面の <b>Reset Token</b> を押して、表示された文字列をコピーする
                <div className="mt-1 text-2xs text-faint">
                  一度しか表示されません。閉じてしまったらもう一度 Reset すれば大丈夫です。
                </div>
              </li>
            </ol>
            <div className="pt-1">
              <Button variant="primary" onClick={() => setStep(1)}>
                コピーできた
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* ── 2. トークンを確かめる ───────────────────── */}
      {step === 1 && (
        <Card title="2. トークンを貼る" desc="正しいか、その場で確かめます。">
          <div className="space-y-3 px-4 py-3">
            <Field
              label="Botトークン"
              hint="Client Secret ではありません。Bot ページの Reset Token で出る長い文字列です。"
            >
              <input
                className={inputClass}
                type="password"
                value={token}
                onChange={(e) => {
                  setToken(e.target.value);
                  // 検証したのは前のトークン。書き換えたら結果ごと無効にする
                  // （古い「つながりました」を残すと、未検証のまま先へ進めてしまう）
                  setBot(null);
                  setInviteUrl("");
                  setIntentWarning("");
                }}
                placeholder="MTIzNDU2Nzg5..."
                autoComplete="off"
              />
            </Field>

            {bot !== null && (
              <div className="rounded border border-accent/30 bg-accent/5 px-3 py-2">
                <div className="flex items-center gap-2">
                  {bot.avatarUrl !== "" && (
                    <img src={bot.avatarUrl} alt="" className="h-8 w-8 rounded-full" />
                  )}
                  <div>
                    <div className="text-xs font-medium">つながりました: {bot.username}</div>
                    <div className="text-2xs text-muted">このBotを使います</div>
                  </div>
                </div>
                {intentWarning !== "" && (
                  <div className="mt-2 rounded bg-amber-500/10 px-2 py-1.5 text-2xs text-amber-700 dark:text-amber-400">
                    ⚠️ {intentWarning}
                  </div>
                )}
                {inviteUrl !== "" && (
                  <p className="mt-2 text-2xs">
                    まだサーバーに入れていない場合は{" "}
                    <a className="text-accent underline" href={inviteUrl} target="_blank" rel="noreferrer">
                      この招待リンク
                    </a>
                    から入れてください。
                  </p>
                )}
              </div>
            )}

            <div className="flex gap-2">
              <Button busy={busy} onClick={() => void verifyToken()} disabled={token.trim() === ""}>
                接続テスト
              </Button>
              {bot !== null && (
                <Button variant="primary" busy={busy} onClick={() => void loadGuilds()}>
                  次へ
                </Button>
              )}
              <Button onClick={() => setStep(0)}>戻る</Button>
            </div>
          </div>
        </Card>
      )}

      {/* ── 3. サーバーとチャンネル ─────────────────── */}
      {step === 2 && (
        <Card title="3. どこに住むか決める" desc="IDを調べる必要はありません。一覧から選んでください。">
          <div className="space-y-3 px-4 py-3">
            <Field label="サーバー">
              <select
                className={inputClass}
                value={guildId}
                onChange={(e) => void loadChannels(e.target.value)}
              >
                <option value="">選んでください</option>
                {guilds.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            </Field>
            {guilds.length === 0 && (
              <p className="text-2xs text-muted">
                サーバーが出てこない場合は、前の画面の招待リンクからBotを入れてください。
              </p>
            )}

            {channels.length > 0 && (
              <Field
                label="ホームチャンネル"
                hint="エージェントが常駐する場所です。ここでは呼ばれなくても答えます。"
              >
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
              </Field>
            )}

            <div className="flex gap-2">
              <Button
                variant="primary"
                busy={busy}
                disabled={channelId === ""}
                onClick={() => void detectLlm()}
              >
                次へ
              </Button>
              <Button onClick={() => setStep(1)}>戻る</Button>
              {/* 押せない理由を書かないと、利用者は何が足りないのか分からない */}
              {channelId === "" && channels.length > 0 && (
                <span className="self-center text-2xs text-muted">
                  ホームチャンネルを選ぶと次へ進めます
                </span>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* ── 4. 使うAI ──────────────────────────────── */}
      {step === 3 && (
        <Card title="4. 使うAIを選ぶ" desc="インストール済みのものだけ選べます。">
          <div className="space-y-3 px-4 py-3">
            <div className="space-y-2">
              {providers.map((p) => (
                <label
                  key={p.name}
                  className={`flex cursor-pointer items-start gap-2 rounded border px-3 py-2 ${
                    provider === p.name ? "border-accent bg-accent/5" : "border-hairline"
                  } ${p.installed ? "" : "opacity-60"}`}
                >
                  <input
                    type="radio"
                    className="mt-1"
                    checked={provider === p.name}
                    disabled={!p.installed}
                    onChange={() => {
                      setProvider(p.name);
                      setModel(p.defaultModel);
                      setLlmReply("");
                    }}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-xs font-medium">{p.label}</span>
                    {p.installed ? (
                      <span className="block text-2xs text-faint">{p.path}</span>
                    ) : (
                      <span className="block text-2xs text-muted">
                        インストールされていません
                        {p.install !== "" && (
                          <>
                            {" — "}
                            <a className="text-accent underline" href={p.install} target="_blank" rel="noreferrer">
                              入手する
                            </a>
                          </>
                        )}
                      </span>
                    )}
                    {!p.rich && p.installed && (
                      <span className="block text-2xs text-muted">
                        添付ファイルの読解・Web検索は使えません
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>

            <Field label="モデル名（空ならツールの既定）">
              <input className={inputClass} value={model} onChange={(e) => setModel(e.target.value)} />
            </Field>

            {llmReply !== "" && (
              <div className="rounded border border-accent/30 bg-accent/5 px-3 py-2 text-xs">
                <div className="font-medium">AIからの返事:</div>
                <div className="mt-1 whitespace-pre-wrap text-muted">{llmReply}</div>
                {llmLimits !== "" && <div className="mt-1.5 text-2xs text-muted">{llmLimits}</div>}
              </div>
            )}

            <div className="flex gap-2">
              <Button busy={busy} onClick={() => void testLlm()}>
                応答テスト
              </Button>
              <Button variant="primary" busy={busy} onClick={() => void loadTemplates()}>
                次へ
              </Button>
              <Button onClick={() => setStep(2)}>戻る</Button>
            </div>
          </div>
        </Card>
      )}

      {/* ── 5. 性格 ───────────────────────────────── */}
      {step === 4 && (
        <Card title="5. 性格を決める" desc="あとから何度でも書き換えられます。">
          <div className="space-y-3 px-4 py-3">
            <div className="grid gap-2 sm:grid-cols-3">
              {templates.map((t) => (
                <label
                  key={t.fileName}
                  className={`cursor-pointer rounded border px-3 py-2 ${
                    template === t.fileName ? "border-accent bg-accent/5" : "border-hairline"
                  }`}
                >
                  <input
                    type="radio"
                    className="sr-only"
                    checked={template === t.fileName}
                    onChange={() => setTemplate(t.fileName)}
                  />
                  <span className="block text-xs font-medium">{t.title}</span>
                  <span className="mt-0.5 block text-2xs text-muted">{t.summary}</span>
                </label>
              ))}
            </div>

            <Field label="名前" hint="Discordでの呼び名になります。">
              <input
                className={inputClass}
                value={agentName}
                onChange={(e) => setAgentName(e.target.value)}
                placeholder="例: あかり"
              />
            </Field>
            <Field label="チーム名（任意）" hint="性格ファイルの中で使われます。">
              <input
                className={inputClass}
                value={teamName}
                onChange={(e) => setTeamName(e.target.value)}
                placeholder="例: 開発チーム"
              />
            </Field>

            <div className="flex gap-2">
              <Button
                variant="primary"
                busy={busy}
                disabled={!canFinish}
                onClick={() => void finish()}
              >
                これで作る
              </Button>
              <Button onClick={() => setStep(3)}>戻る</Button>
            </div>
          </div>
        </Card>
      )}

      {/* ── 6. 完了 ───────────────────────────────── */}
      {step === 5 && (
        <Card title="できました" desc="Discordを見てください。">
          <div className="space-y-3 px-4 py-3 text-xs">
            <p className="text-sm">
              🎉 <b>{agentName}</b> がチャンネルに挨拶しました。
            </p>
            {launchWarning !== "" && (
              <div className="rounded bg-amber-500/10 px-3 py-2 text-2xs leading-relaxed text-amber-700 dark:text-amber-400">
                ⚠️ 挨拶は届きましたが、BOT本体の起動には失敗しました: {launchWarning}
                <br />
                ターミナルで <code>python run.py</code> を実行するか、
                「運用」画面から起動してください（このままでは話しかけても返事が来ません）。
              </div>
            )}
            {jumpUrl !== "" && (
              <p>
                <a className="text-accent underline" href={jumpUrl} target="_blank" rel="noreferrer">
                  Discordで確認する
                </a>
              </p>
            )}
            <div className="rounded bg-hairline/40 px-3 py-2 text-2xs leading-relaxed">
              <b>次にやるとよいこと</b>
              <ul className="ml-4 mt-1 list-disc space-y-0.5">
                <li>チャンネルで話しかけてみる（過去ログを検索して答えます）</li>
                <li>「全体設定」で自発的な行動をオンにする</li>
                <li>「運用」からPC起動時の自動立ち上げを設定する</li>
              </ul>
            </div>
            <Button variant="primary" onClick={onDone}>
              管理画面へ
            </Button>
          </div>
        </Card>
      )}
    </div>
  );
}
