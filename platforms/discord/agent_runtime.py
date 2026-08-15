#!/usr/bin/env python3
"""
マルチエージェントBotの共有ランタイム — 設定・共有状態・純粋ヘルパー。

bot.py（AgentClientの本体）と webhook_personas.py（試用枠人格のmixin）が
共通で参照する土台をここに集約する。ここには discord.Client も AgentClient も
置かない（副作用の小さい設定・状態・純粋関数だけ）。

共有可変状態（AGENT_USER_IDS / WEBHOOK_AGENTS / PLUGINS 等）は「再束縛せず
中身を入れ替える」規約にする。こうすると `from agent_runtime import X` で
取り込んだ側も同じオブジェクトを見続けられる（束縛のズレを防ぐ）。
"""

import os
import re
import sys
import json
import asyncio
import contextlib

import discord

from core import config as app_config
from core import db
from core import invoke_claude
from core import paths
from core import integrations
from core import plugins
from core import thread_reply

# パスは core.paths に集約している（ここで組み立てないこと）
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.ROOT
CONFIG_PATH = paths.CONFIG_PATH
DB_PATH = paths.DB_PATH

#: config内の相対パス（persona_files 等）をリポジトリルート基準の絶対パスへ
_resolve = paths.resolve


# 設定の読み込みと検査は core.config が持つ（BOTごとに書かない）。
# 過去の呼び出し名を残しておく（テストと開発BOTが参照している）
EMPTY_CONFIG = app_config.EMPTY_CONFIG
load_config = app_config.load
validate_config = app_config.validate


config = load_config()
GUILD_ID = int(config.get("guild_id") or 0)
# 共有ルール（global）の設定・他人のルール削除を許す管理者のDiscord user id
ADMIN_IDS = {str(x) for x in config.get("admins", [])}
# AI人事が新チャンネルを作る際のカテゴリ（AIエージェント関連をまとめる）
AGENT_CATEGORY_ID = config.get("agent_category_id")
# 外部連携（integrations/ 配下）。config の integrations.enabled に
# 書いたものだけを読み込む。1件が壊れていても他は生きる（integrations.py 参照）
INTEGRATIONS = integrations.load(config)
HISTORY_LIMIT = int(config.get("history_limit", 10))
# 「直近の会話」として遡って読むメッセージ件数。会話の連続性を追えるよう広めに
# 取る（history_limit はループガードの窓なので小さめで良く、意味が別なので分離）。
# ループガード用の履歴取得もこれで賄う（多めに読んでも trailing_bot_count は
# 末尾のBot連続数だけ数えるので安全）。history_limit を下回らないよう max で下支え。
CONTEXT_HISTORY_LIMIT = max(int(config.get("context_history_limit", 30)),
                           HISTORY_LIMIT)
# Bot同士の会話の上限: 直近履歴の連続Bot発言数+1 がこの値以上なら沈黙
MAX_BOT_CHAIN = int(config.get("max_bot_chain", 4))
# claude CLI の同時実行数（1回答 = CLI 2回呼び出し）
ANSWER_SEM = asyncio.Semaphore(int(config.get("max_concurrent_answers", 2)))
# 画像生成は直列化（外部サービスを同時に叩きすぎないため）
IMAGE_SEM = asyncio.Semaphore(1)

AGENTS = config["agents"]
AGENTS_BY_ID = {a["id"]: a for a in AGENTS}
# agent_id -> Bot user id（on_readyでランタイム収集。configに書かせない）
AGENT_USER_IDS = {}

# Webhook人格（試用枠。エージェントv2 Phase 2）。archiver=アーカイブ担当が受信を代行し、
# 投稿だけWebhookで名前・アイコンを差し替える。既存3体（本物Bot）とは独立。
WEBHOOK_AGENTS = {}           # id -> 台帳行(dict)
WEBHOOK_HOME_CHANNELS = {}    # home_channel_id -> id
_webhook_cache = {}          # channel_id -> discord.Webhook（毎回作らない）

# プラグインスキル（Phase 3: builderが tools/ に生やす能力）。MARKER -> module
# 再束縛せず中身を入れ替える（load_pluginsでclear+update）
PLUGINS = {}

# 文脈要約のチャンネル単位ロック（複数エージェントが同一chで同時応答しても
# 要約更新を直列化する。インスタンス単位だと3体が別ロックで競合するためモジュール
# レベルで共有する）
SUMMARY_LOCKS = {}


def load_plugins():
    """tools/ のプラグインを読み込む（起動時＋有効化後に呼ぶ・再起動不要）。
    PLUGINS はモジュール間で共有するため、再束縛せず中身を入れ替える。"""
    loaded = plugins.load_plugins()
    PLUGINS.clear()
    PLUGINS.update(loaded)


IMAGE_MARKER_RE = re.compile(r"\[IMAGE:\s*([^\]]+)\]")
CAPTION_MARKER_RE = re.compile(r"\[CAPTION:\s*([^\]]+)\]")

# フィードバック（物差しの原料）: エージェントの投稿への評価リアクション。
# ❤ は異体字セレクタ有無の両方を入れる（クライアントで送出形が異なる）
POSITIVE_REACTIONS = {"👍", "✅", "💯", "🙆", "❤️", "❤", "🎉", "👏"}
NEGATIVE_REACTIONS = {"👎", "❌", "🙅"}
# 採用の承認は👍のみ（提案文と一致。何気ない❤️等で確定させない）
APPROVE_REACTIONS = {"👍"}

ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


def registered_bot_ids():
    """ready済みエージェントのBot user id集合。"""
    return set(AGENT_USER_IDS.values())


def _has_skill(wa, skill):
    """台帳エージェントが指定スキルを持つか（skills_json をパース）。"""
    try:
        return bool(json.loads(wa.get("skills_json") or "{}").get(skill))
    except (ValueError, TypeError):
        return False


def _current_roster():
    """現メンバー（本物Bot＋稼働中Webhook人格）の (名前, 役割) 一覧。
    AI人事が「既存で足りるか」を判断するためにプロンプトへ載せる。"""
    roster = [(a["name"], a.get("role") or "なんでも相談") for a in AGENTS]
    for wa in WEBHOOK_AGENTS.values():
        roster.append((wa["name"], "（試用枠）"))
    return roster


def _load_local_avatar(avatar_url):
    """avatar_url がローカルパス（http以外）なら画像バイトを返す。
    URL・未設定・読めない場合は None。"""
    if not avatar_url or str(avatar_url).startswith(("http://", "https://")):
        return None
    path = _resolve(avatar_url)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        print(f"avatar not found: {path}")
        return None


def _load_webhook_agents():
    """台帳(agentsテーブル)からactiveなWebhook人格を読み込む。
    不正な行があってもアーカイブ担当の中核ループを止めないよう握りつぶす。"""
    WEBHOOK_AGENTS.clear()
    WEBHOOK_HOME_CHANNELS.clear()
    try:
        with db.connect(DB_PATH) as conn:
            agents = db.get_active_agents(conn)
        for a in agents:
            try:
                WEBHOOK_HOME_CHANNELS[int(a["home_channel_id"])] = a["id"]
                # avatar_url がローカルパスなら画像バイトを先読み（Webhook本体の
                # アイコンに使う）。http(s) はそのまま per-message URL に使う
                a["avatar_bytes"] = _load_local_avatar(a.get("avatar_url"))
                WEBHOOK_AGENTS[a["id"]] = a
            except (TypeError, ValueError):
                print(f"skip invalid webhook agent row: {a.get('id')}")
    except Exception as e:
        print(f"webhook agents load failed: {e}")
    if WEBHOOK_AGENTS:
        print(f"webhook personas: "
              f"{[(a['id'], a['name']) for a in WEBHOOK_AGENTS.values()]}")


def should_webhook_respond(*, has_webhook_agents, home_agent_id, webhook_id,
                           author_is_bot, author_id, self_user_id,
                           mention_ids, registered_ids, text):
    """Webhook人格が応答すべきか（純粋関数・テスト対象。ループ安全性の要）。
    - 人格未登録 / ホームchでない → False
    - Webhook投稿(自人格含む) / Bot / 自分の発言 → False（無限ループ遮断）
    - 別の本物エージェントが名指し → False（譲り合い）
    - テキスト無し → False"""
    if not has_webhook_agents or home_agent_id is None:
        return False
    if webhook_id is not None or author_is_bot:
        return False
    if self_user_id is not None and author_id == self_user_id:
        return False
    if any(m in registered_ids for m in mention_ids):
        return False
    return bool((text or "").strip())


def build_roster_note(self_agent_id):
    """自分以外のready済みエージェント一覧＋相談ルールのプロンプト注入文。"""
    lines = []
    for aid, user_id in AGENT_USER_IDS.items():
        if aid == self_agent_id:
            continue
        a = AGENTS_BY_ID[aid]
        role = a.get("role") or "なんでも相談"
        lines.append(f"- {a['name']} <@{user_id}>: {role}"
                     f"（ホーム: <#{a['home_channel_id']}>）")
    if not lines:
        return ""
    return (
        "【同僚AIエージェント】\n" + "\n".join(lines) + "\n"
        "本当に相手の専門観点が必要な時だけ、返信本文に上記の <@ユーザーID> を"
        "そのまま含めてメンション相談できる（相手が返信してくれる）。"
        "Bot同士の連続やりとりは上限で自動停止するため、乱用しないこと。"
    )


IMAGE_SKILL_NOTE = (
    "【画像生成スキル】あなたは画像を生成できる。依頼が画像・ビジュアル案の"
    "生成を求めていると判断したら、返信本文の最後に改行して次の2行を"
    "追加すること（本文には何を作るか一言添える）:\n"
    "[IMAGE: 詳細な画像生成プロンプト]\n"
    "[CAPTION: 完成した画像と一緒に投稿する報告の一言（あなたの口調で、"
    "どんな仕上がりにしたかを短く）]\n"
    "修正依頼のときは直近の会話の要件を統合した新しいプロンプトを書く。"
    "画像が不要な相談には絶対に付けないこと。\n"
    "依頼メッセージ（またはそのリプライ先）に画像が添付されている場合、"
    "その画像は生成時に自動で参考画像として画像生成AIに渡される。"
    "[IMAGE:] プロンプトにはReadで確認した添付画像の内容を踏まえ、"
    "「添付の参考画像のテイスト/人物/構図を◯◯して」のように参考画像への"
    "言及を必ず含めること。添付もリプライも無いのに過去の画像を参照したい"
    "依頼のときは、生成せずに画像の添付し直しかその画像へのリプライを"
    "促すこと。"
)

# YouTube要約（youtube_summary.py）。発動はbot側の正規表現で決定的に行う
# ため、これはLLMへの能力告知のみ（自分の能力を否定させない）
YOUTUBE_SKILL_NOTE = (
    "【YouTube要約スキル】YouTubeのURLをあなたのホームchに投稿"
    "（他chでは自分宛メンション+URL）してもらえば、動画の字幕から"
    "自動で要約できる。使い方を聞かれたらそう案内すること。"
)

# PDF自動要約（pdf_summary.py）。発動はbot側で決定的に行うため、これは
# LLMへの能力告知のみ（自分の能力を否定させない）
PDF_SKILL_NOTE = (
    "【PDF自動要約スキル】サーバー内のチャンネルにPDFが添付投稿されると、"
    "メンション不要で自動的に中身を要約して投稿する。"
    "使い方を聞かれたらそう案内すること。"
)


@contextlib.asynccontextmanager
async def _best_effort_typing(channel):
    """typing表示はベストエフォート。開始に失敗（Discordの一時500等）しても
    本処理（回答生成・投稿）を止めない。"""
    cm = channel.typing()
    entered = False
    try:
        await cm.__aenter__()
        entered = True
    except Exception as e:
        print(f"typing start skipped: {e}")
    try:
        yield
    finally:
        if entered:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


async def _send_chunked(channel, text):
    """Discordの2000文字制限に合わせて分割送信。"""
    limit = 1900
    for i in range(0, len(text), limit):
        await channel.send(text[i:i + limit],
                           allowed_mentions=ALLOWED_MENTIONS)


async def _open_thread(message, name):
    """対象messageからスレッドを開始（既存があれば再利用）して返す。
    既にスレッド内・作成失敗（権限不足やDiscordの一時エラー等）時は元chを
    そのまま返す＝「スレッド化できなくても従来通り必ず返信する」を保証する。"""
    channel = message.channel
    if isinstance(channel, discord.Thread):
        return channel                       # 既にスレッド内なら入れ子にしない
    existing = getattr(message, "thread", None)
    if existing is not None:
        return existing                      # この投稿に紐づく既存スレッド
    try:
        return await message.create_thread(name=name)
    except Exception as e:
        print(f"thread open skipped: {e}")
        return channel


def _log_shadow(agent_id, message, kind, name):
    """シャドー記録: 実際にはスレッドを開かず「開くつもりだった」を proactive_log
    に残す（本文は元chへ投稿）。記録失敗は本流（返信）を止めない。proactive は
    重い依存を引くため遅延importする（agent_runtime を軽く保つ）。"""
    try:
        from core import proactive
        proactive.log_entry(
            DB_PATH, agent_id, kind="thread", action="shadow_open",
            channel_id=getattr(message.channel, "id", None),
            trigger_message_id=getattr(message, "id", None),
            detail=f"{kind}:{name}"[:100])
    except Exception as e:
        print(f"thread shadow log skipped: {e}")


async def deliver_reply(message, body, *, cfg, kind, agent_id):
    """返信の投稿先を thread_reply 設定で切り替えて body を送る。戻り値=送信先。

    - 無効/対象外/Bot発言 → 元ch（従来の直接投稿）
    - shadow（既定） → 元chへ投稿しつつ、開くはずだったスレッドを記録するだけ
    - 実投稿 → 対象messageからスレッドを開始し、その中へ投稿する
    どの分岐でも本文は必ずどこかへ届く（スレッド化は失敗時に元chへ退避）。"""
    channel = message.channel
    is_bot = bool(getattr(getattr(message, "author", None), "bot", False))
    if not thread_reply.wants(cfg, kind, is_bot=is_bot):
        await _send_chunked(channel, body)
        return channel
    name = thread_reply.thread_name(
        getattr(message, "clean_content", "") or "", kind=kind)
    if cfg.get("shadow", True):
        await asyncio.to_thread(_log_shadow, agent_id, message, kind, name)
        await _send_chunked(channel, body)
        return channel
    target = await _open_thread(message, name)
    await _send_chunked(target, body)
    return target


def _content_gate(trigger, text, has_attachments):
    """発言を処理してよいか。テキストがあれば常に通す。
    添付のみ（テキスト空）はメンション経由のみ通す: ホームchの無言添付まで
    通すと、気軽なスクショ投下に毎回AIがコメントしてノイズ化するため。"""
    if (text or "").strip():
        return True
    if not has_attachments:
        return False
    return trigger in ("human_mention", "agent_mention")


async def _collect_attachments(message):
    """本文の添付＋リプライ先の添付をID重複排除で結合（本文の添付が先）。
    リプライ先の取得はbest-effort（削除済み・取得失敗は黙って空扱い）。"""
    atts = list(message.attachments)
    ref = message.reference
    if ref and ref.message_id:
        target = ref.resolved
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except Exception:
                target = None
        if isinstance(target, discord.DeletedReferencedMessage):
            target = None
        if target is not None:
            atts.extend(getattr(target, "attachments", None) or [])
    seen, out = set(), []
    for att in atts:
        if att.id in seen:
            continue
        seen.add(att.id)
        out.append(att)
    return out


def resolve_runner_enabled(agent, unavailable=None):
    """runner経路を使うか。未指定なら「使えるならON」（既定ON）。

    明示的に true/false が書かれていればそれに従う。書かれていないときだけ、
    claude CLI が使える状態かで決める。runner は Claude Code 専用の経路で、
    他プロバイダでは invoke_claude が RuntimeError を投げて回答が落ちるため、
    既定ONにするには「使えない環境では静かに旧経路へ寄せる」がセットで要る。

    unavailable: 使えない理由（None なら使える）。テスト用の差し込み口。
    """
    explicit = agent.get("runner_enabled")
    if explicit is not None:
        return bool(explicit)
    if unavailable is None:
        unavailable = invoke_claude.check_available()
    return unavailable is None


def resolve_session_resume(agent, runner_enabled):
    """会話セッション継続を使うか。未指定なら runner経路と同じ既定ON。

    resume は runner経路の上でしか動かないので、runner が旧経路に落ちた
    ときは自動的にオフになる（設定を書き換えずに整合する）。
    """
    cfg = agent.get("session_resume") or {}
    return bool(cfg.get("enabled", True)) and runner_enabled


async def fetch_history(message):
    """chの直近メッセージを古い順で返す（現在の発言は除く）。best-effort。
    空content（画像のみ等）も残す: ループガードの連続Botカウントに必要。"""
    try:
        items = []
        async for m in message.channel.history(limit=CONTEXT_HISTORY_LIMIT,
                                               before=message):
            items.append({
                "id": m.id,
                "author": m.author.display_name,
                "content": (m.clean_content or "").strip(),
                "is_bot": m.author.bot,
            })
        items.reverse()  # 古い順に並べ替え
        return items
    except Exception as e:
        print(f"history fetch failed: {e}")
        return []


def trailing_bot_count(history):
    """history（古い順）の末尾から is_bot が連続する件数。"""
    count = 0
    for item in reversed(history):
        if not item["is_bot"]:
            break
        count += 1
    return count


def _resolve_mention(guild, to_text):
    """リマインダーの to=宛先名 をメンション文字列に解決する。
    everyone/ロール名/メンバー表示名の順。同名複数（曖昧）は誤送信を
    避けるため解決しない。(mention, 表示ラベル) か (None, None)。"""
    name = (to_text or "").strip().lstrip("@")
    if guild is None or not name:
        return None, None
    if name.lower() in ("everyone", "全員", "全体"):
        return "@everyone", "@everyone"
    roles = [r for r in guild.roles if r.name == name]
    if len(roles) == 1:
        return roles[0].mention, f"@{roles[0].name}"
    members = [m for m in guild.members
               if m.display_name == name or m.name == name]
    if not roles and len(members) == 1:
        return members[0].mention, f"@{members[0].display_name}"
    return None, None


# 宛先の区切り: カンマ・読点・スラッシュ・中点（「と」は名前に含まれ得るため不使用）
_TO_SPLIT_RE = re.compile(r"[,、/・]+")
_TERM_ID_RE = re.compile(r"Discord\s*ID[:：]\s*([\w.\-]+)")
_TERM_ALIAS_RE = re.compile(r"別名[:：]\s*([^／]+)")


def _terms_username(name, terms_rows):
    """固有名詞辞書（人間提供の対応表）から名前→Discordユーザー名を引く。
    表記そのもの・別名のどちらでも一致（純粋関数）。無ければ None。"""
    name = (name or "").strip()
    for row in terms_rows or []:
        desc = row.get("description") or ""
        m = _TERM_ID_RE.search(desc)
        if not m:
            continue
        aliases = {row.get("term") or ""}
        am = _TERM_ALIAS_RE.search(desc)
        if am:
            aliases.update(a.strip() for a in re.split(r"[、,]", am.group(1)))
        if name in aliases:
            return m.group(1)
    return None


def resolve_mentions(guild, to_text, terms_rows=None):
    """to=宛先（複数可・カンマ区切り）をメンションへ解決する。
    各名前は 固有名詞辞書（人間提供の対応表が最優先。同名の別人誤爆を防ぐ）
    → guild照合（既存 _resolve_mention）の順で解決する。
    Returns: (連結mention or None, 連結ラベル or None, 未解決名リスト)。"""
    parts = [p.strip().lstrip("@") for p in
             _TO_SPLIT_RE.split(to_text or "") if p.strip().lstrip("@")]
    mentions, labels, unresolved = [], [], []
    for name in parts:
        username = _terms_username(name, terms_rows)
        if username and guild is not None:
            members = [m for m in guild.members if m.name == username]
            if len(members) == 1:
                mentions.append(members[0].mention)
                labels.append(f"@{name}")
                continue
        mention, label = _resolve_mention(guild, name)
        if mention:
            mentions.append(mention)
            labels.append(label)
        else:
            unresolved.append(name)
    if not mentions:
        return None, None, unresolved
    # 同一人物の重複（別名で2回指定など）は1つに
    seen, uniq_m, uniq_l = set(), [], []
    for m, l in zip(mentions, labels):
        if m not in seen:
            seen.add(m)
            uniq_m.append(m)
            uniq_l.append(l)
    return " ".join(uniq_m), " ".join(uniq_l), unresolved


def _is_broadcast(mention):
    """everyone/ロール宛（多人数に通知が飛ぶメンション）を含むか。
    複数宛先の連結文字列にも対応する。"""
    return "@everyone" in mention or "<@&" in mention


# チャンネル宛（起票#4）: <#123>（Discordの自動変換）か #名前 のトークンだけを扱う
_CHANNEL_MENTION_RE = re.compile(r"^<#(\d+)>$")


def split_channel_tokens(to_text):
    """to=宛先を チャンネル指定（#名前 / <#id>）と人宛の残りに分ける（純粋関数）。
    Returns: (チャンネルトークンのリスト, 人宛の残りテキスト)。"""
    parts = [p.strip() for p in _TO_SPLIT_RE.split(to_text or "") if p.strip()]
    chans, people = [], []
    for p in parts:
        if p.startswith("#") or _CHANNEL_MENTION_RE.match(p):
            chans.append(p)
        else:
            people.append(p)
    return chans, "、".join(people)


def resolve_channel(guild, token):
    """#名前 / <#id> をguildのテキストチャンネルへ解決する。
    見つからない・同名複数は None（誤爆よりも不発を選ぶ）。"""
    if guild is None:
        return None
    m = _CHANNEL_MENTION_RE.match(token)
    if m:
        return guild.get_channel(int(m.group(1)))
    name = token.lstrip("#").strip()
    if not name:
        return None
    found = [c for c in getattr(guild, "text_channels", [])
             if c.name == name]
    return found[0] if len(found) == 1 else None


def can_target_channel(channel, member):
    """別チャンネル宛リマインダーを設定してよいか: 依頼者本人がそのchを
    見えて書き込めることを条件にする（見えない/書けないchへの代理投稿を防ぐ）。"""
    try:
        perms = channel.permissions_for(member)
    except Exception:
        return False
    return bool(getattr(perms, "view_channel", False)
                and getattr(perms, "send_messages", False))


def _can_broadcast(member):
    """everyone/ロール宛リマインダーを設定してよい人か。
    Discord本来の「@everyone、@here、全ロールにメンション」権限に揃える。"""
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.mention_everyone)


MAX_REF_IMAGES = 4  # 画像生成へ渡す参考画像の上限


def load_imagegen(cfg):
    """画像生成バックエンドを返す（無ければ None）。

    バックエンド本体はこのリポジトリには同梱していない。画像生成APIは
    サービスごとに規約も課金も異なるため、**外部連携として利用者が入れる**
    設計にしている（docs/07-integrations.md）。

    config の `image_gen.backend` に連携名を書き、その連携が
        generate(prompt, cfg, ref_images=None) -> 画像ファイルのパス
    を公開していれば使われる。既定は "imagegen"。
    """
    name = (cfg or {}).get("backend") or "imagegen"
    for i in INTEGRATIONS:
        if i.name == name:
            if hasattr(i.module, "generate"):
                return i.module
            print(f"integration {name}: generate() が無いため画像生成に使えません")
            return None
    return None


def collect_ref_image_paths(saved):
    """download()済みの添付から、画像生成の参考画像に使うパスを選ぶ。
    画像のみ・先頭優先（本文の添付がリプライ先より先）・上限あり。"""
    return [s["path"] for s in saved
            if s["kind"] == "image"][:MAX_REF_IMAGES]


def extract_image_marker(answer):
    """回答から [IMAGE: ...] と [CAPTION: ...] マーカーを分離して
    (本文, プロンプト|None, キャプション|None) を返す。"""
    m = IMAGE_MARKER_RE.search(answer)
    if not m:
        return answer, None, None
    c = CAPTION_MARKER_RE.search(answer)
    text = CAPTION_MARKER_RE.sub("", IMAGE_MARKER_RE.sub("", answer)).strip()
    return text, m.group(1).strip(), (c.group(1).strip() if c else None)
