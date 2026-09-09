#!/usr/bin/env python3
"""
Discord マルチエージェントBot — アーカイブ基盤 + チャンネル専属AIエージェント

1プロセスで複数の discord.Client（エージェント）を同一イベントループで動かす。
- アーカイブ（全ch保存・backfill・編集/削除反映）は archiver: true の1体だけが行う
- 各エージェントは自分のホームchで人間の発言に回答する
- 人間からの自分宛メンションには guild 内のどこでも回答する
- 登録エージェント同士は相互メンションで会話できる（連続Bot発言の上限で自動停止）
- 回答は共通の archive.db を検索して生成（共有ナレッジ）

このファイルは AgentClient 本体（本物Botの中核挙動）と起動処理に責務を絞る。
設定・共有状態・純粋ヘルパーは agent_runtime、アーカイブ保存は archiving、
試用枠Webhook人格の応答/人事は webhook_personas に分離している。
"""

import sys
import os
import asyncio
import contextlib
import logging

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
os.environ["PYTHONUNBUFFERED"] = "1"

from core import logstamp  # noqa: E402
logstamp.install()  # ログの各行に時刻（v4 Phase 0 計測）

import discord

from core import action_items
from core import attachments
from core import config as app_config
from core import db
from core import episodes
from core import wiki
from core import glossary
from core import heartbeat
from core import integrations
from core import invoke_claude
from core import msgref
from core import plugins
from core import proactive
from core import profiles
from core import reminders
from core import rules
from core import sessions
from core import runner_answer
from core import search
from core import self_review
from core import selfreview_distill
from core import summaries
from core import thread_reply

from platforms.discord import archiving
from platforms.discord.agent_loops import AgentLoopsMixin
from platforms.discord.marker_actions import MarkerActionsMixin
from platforms.discord.reaction_handlers import ReactionHandlersMixin
from platforms.discord.skill_hooks import SkillHooksMixin
from platforms.discord.tool_loop import ToolLoopMixin
from core.archive_tools import evidence as tool_evidence
from core.archive_tools import launch as tool_launch
from platforms.discord.webhook_personas import WebhookPersonaMixin
from platforms.discord import agent_runtime
from platforms.discord.agent_runtime import (
    ADMIN_IDS,
    AGENT_USER_IDS,
    AGENTS,
    ALLOWED_MENTIONS,
    ANSWER_SEM,
    DB_PATH,
    GUILD_ID,
    IMAGE_SEM,
    IMAGE_SKILL_NOTE,
    MAX_BOT_CHAIN,
    PDF_SKILL_NOTE,
    SUMMARY_LOCKS,
    YOUTUBE_SKILL_NOTE,
    _best_effort_typing,
    _collect_attachments,
    _content_gate,  # noqa: F401 （テスト互換のため再エクスポート）
    _resolve,
    build_roster_note,
    collect_ref_image_paths,
    extract_image_marker,  # noqa: F401 （テスト互換のため再エクスポート）
    fetch_history,
    intents,
    load_imagegen,
    load_plugins,
    registered_bot_ids,
    trailing_bot_count,
)

#: スーパーバイザが鮮度を見るときの名前（core/supervisor.py の ServiceDef と揃える）
HEARTBEAT_ID = "archivebot"
#: 生存証明を書く間隔。失効閾値（既定300秒）より十分短くする
HEARTBEAT_INTERVAL_SEC = 60

# 一部のテストは bot.<symbol> で参照する。上の import で bot 名前空間に
# 束縛されるものに加え、本体では使わなくなった（mixinへ移った）ヘルパーも
# テスト互換のためここで再エクスポートする。
_can_broadcast = agent_runtime._can_broadcast
_is_broadcast = agent_runtime._is_broadcast
_resolve_mention = agent_runtime._resolve_mention

# PLUGINS はモジュール間共有のため agent_runtime.PLUGINS を直接見る
# （load_plugins が中身を入れ替える）


def normalize_name_call(raw):
    """agents[].name_call を {enabled, shadow, aliases} に正規化する（純粋関数）。
    dict 以外（トグル事故で true が書かれた形）は既定＝オフに倒す。"""
    nc = raw if isinstance(raw, dict) else {}
    aliases = [str(x).strip() for x in (nc.get("aliases") or [])
               if str(x).strip()]
    return {"enabled": bool(nc.get("enabled", False)),
            "shadow": bool(nc.get("shadow", True)),
            "aliases": aliases}


def name_call_allowed_here(channel_id, other_home_ids, excluded_ids):
    """名前呼びに応答してよい ch か（純粋関数）。他エージェントのホーム ch と
    観察の除外 ch では応答しない。"""
    ch = str(channel_id)
    return ch not in other_home_ids and ch not in excluded_ids


class AgentClient(ToolLoopMixin, SkillHooksMixin, MarkerActionsMixin,
                  AgentLoopsMixin,
                  ReactionHandlersMixin, WebhookPersonaMixin, discord.Client):
    """1エージェント = 1 Botアカウント = 1クライアント。

    本体（このファイル）はトリガー判定・応答フロー・起動処理だけを持ち、
    周辺機能は責務ごとの mixin に分離している:
      SkillHooksMixin      = プリフック型スキル（メール私書箱/YouTube/PDF）
      MarkerActionsMixin   = マーカー実行（RULE/REMIND/PROACTIVE_QUOTA）
      AgentLoopsMixin      = 常駐ループ（リマインダー・観察ループ＋サイクル登録）
      ReactionHandlersMixin= リアクション処理（feedback/教訓帳/納期の✅❌）
      WebhookPersonaMixin  = 試用枠Webhook人格の応答/人事
    新機能は原則モジュール＋該当mixinへ。このファイルには足さない。"""

    def __init__(self, agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self.is_archiver = bool(agent.get("archiver"))
        self.home_channel_id = int(agent["home_channel_id"])
        self._session_locks = {}   # channel_id -> asyncio.Lock（同時resume防止）
        self.persona_files = [_resolve(p) for p in agent["persona_files"]]
        # image_gen はオブジェクトの存在＝有効。ただし
        #   - {"enabled": false} で設定を残したまま無効化できる（ダッシュボードのトグル）
        #   - dict以外（トグル事故で true が書かれた形）は「既定値で有効」に倒す
        _ig = (agent.get("skills") or {}).get("image_gen")
        if _ig and not isinstance(_ig, dict):
            _ig = {}
        self.image_gen = (_ig if isinstance(_ig, dict)
                          and _ig.get("enabled", True) else None)
        self.reminder = bool((agent.get("skills") or {}).get("reminder"))
        self.youtube_summary = bool(
            (agent.get("skills") or {}).get("youtube_summary"))
        self.pdf_summary = bool(
            (agent.get("skills") or {}).get("pdf_summary"))
        # 外部連携（integrations/）: 二重ゲート＝
        #   グローバルに読み込み済み（config の integrations.enabled）×
        #   このエージェントの skills.<SKILL_KEY> が true
        # 有効な連携が無ければ空リストになり、関連する処理は全て素通りする
        self.integrations = integrations.for_agent(
            agent_runtime.INTEGRATIONS, agent)
        # スレッド返信（能力起票 #12）: 対象投稿からスレッドを開始して返す。
        # 外向き挙動変更ゆえ既定オフ＋シャドー（normalizeで既定化）
        self.thread_reply_cfg = thread_reply.normalize(
            agent.get("thread_reply"))
        # True: ホームchでもメンション必須（チャンネルを静かに保つ）
        self.require_mention = bool(agent.get("require_mention"))
        # 名前呼び: ID メンション無しで「〇〇、これ見て」と呼び名で呼ばれた人間の
        # 発言にも応答する。既定オフ＋シャドー（記録のみ）。名前呼びの取りこぼし対策
        self.name_call_cfg = normalize_name_call(agent.get("name_call"))
        # 自発性の層（v3 Phase A）: 観察ループの設定（無ければ機能オフ）
        self.proactive_cfg = agent.get("proactive") or {}
        # 納期追跡の会話スキル: 議事録追跡を持つエージェントだけ
        # （minutes_channel_id の有無＝_minutes_cycle と同じゲート・新設定なし）
        self.action_tracking = bool(self.proactive_cfg.get(
            "minutes_channel_id"))
        # 投稿セルフレビュー（RM#14・シャドー計測）の設定
        self.self_review_cfg = agent.get("self_review") or {}
        # True: 回答生成を runner/invoke_claude 経由にする（エージェントv2
        # Phase 0）。フラグを外せば旧経路（search.answer_question）に即戻る。
        # **未指定は「使えるならON」**: この経路は claude CLI 専用で、
        # 他プロバイダでは invoke_claude が RuntimeError を投げて回答自体が
        # 落ちる。既定で有効にする以上、使えない環境では黙って旧経路に寄せる
        # （明示 true のときは従来どおり理由を出して失敗させる）
        self.runner_enabled = agent_runtime.resolve_runner_enabled(agent)
        # ツールループ（v4 Phase 1）: 回答の途中で社内データを自分で引く。
        # 既定オフ＋シャドー（証拠行は記録のみ・write は dry-run）。
        # 配線は platforms/discord/tool_loop.py（ToolLoopMixin）
        self.tool_loop_cfg = tool_launch.normalize(agent.get("tool_loop"))
        # create_task はイベントループが弱参照しか持たないため、
        # 参照を保持しないと実行途中でGC回収され得る（要約更新タスク用）
        self._bg_tasks = set()

    async def _heartbeat_loop(self):
        """スーパーバイザ向けの生存証明を定期的に書く。

        **プロセスが生きていること**と**イベントループが回っていること**は
        別物で、過去に問題になったのは後者（プロセスは残ったまま無反応）。
        この小さなループが回り続けている＝ループが詰まっていない証拠になる。
        """
        while True:
            heartbeat.touch(HEARTBEAT_ID)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

    async def on_ready(self):
        AGENT_USER_IDS[self.agent["id"]] = self.user.id
        # 生存証明は archiver 1体だけが書く（3体が同じファイルを奪い合わない）
        if self.is_archiver and not getattr(self, "_heartbeat_task", None):
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        print(f"[{self.agent['id']}] logged in as {self.user}"
              + (" (runner enabled)" if self.runner_enabled else ""))
        if self.reminder and not getattr(self, "_reminder_task", None):
            self._reminder_task = asyncio.create_task(self._reminder_loop())
            print(f"[{self.agent['id']}] reminder loop started")
        if (self.proactive_cfg.get("enabled")
                and not getattr(self, "_proactive_task", None)):
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            print(f"[{self.agent['id']}] proactive loop started "
                  f"(every {self.proactive_cfg.get('interval_min', 30)}min, "
                  f"quota {self.proactive_cfg.get('daily_quota', 3)}/day)")
        if not self.is_archiver:
            return
        db.init_db(DB_PATH)
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            print(f"[{self.agent['id']}] guild {GUILD_ID} not found")
            return
        print(f"backfilling guild: {guild.name}")
        # このアーカイブがどのサーバーのものかを覚える（起動時の突き合わせ用）
        with db.connect(DB_PATH) as conn:
            db.remember_archive_guild(conn, GUILD_ID)
        await archiving.backfill(guild, DB_PATH)
        with db.connect(DB_PATH) as conn:
            print("stats:", db.stats(conn))
        agent_runtime._load_webhook_agents()
        load_plugins()
        print("live capture active.")

    def _trigger(self, message):
        """応答すべきか判定。"home" | "human_mention" | "agent_mention" | None"""
        if self.user is None or message.author.id == self.user.id:
            return None  # 自分の発言には絶対に反応しない
        mentioned = any(u.id == self.user.id for u in message.mentions)
        if message.author.bot:
            # 登録エージェントからの自分宛メンションのみ応答。
            # それ以外のBot・webhook（議事録bot等）は無視
            if mentioned and message.author.id in registered_bot_ids():
                return "agent_mention"
            return None
        if mentioned:
            return "human_mention"  # guild内のどこでも応答
        if self._name_called(message):
            if self.name_call_cfg["shadow"]:
                proactive.log_entry(
                    DB_PATH, self.agent["id"], kind="namecall",
                    action="shadow", channel_id=message.channel.id,
                    trigger_message_id=message.id,
                    detail=(message.clean_content or "")[:120])
                return None
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="namecall", action="used",
                channel_id=message.channel.id, trigger_message_id=message.id,
                detail=(message.clean_content or "")[:120])
            return "human_mention"
        if self.require_mention:
            return None  # メンション必須エージェントはここまで
        if message.channel.id == self.home_channel_id:
            # 他の登録エージェントが名指しされていたら出しゃばらない
            if any(u.id in registered_bot_ids() for u in message.mentions):
                return None
            return "home"
        return None

    def _name_called(self, message):
        """人間の発言に自分の呼び名が含まれるか（ID メンション無し・他エージェントの
        名指し無し・ホーム ch 以外でも）。他のエージェントのホーム ch と観察の
        除外 ch では出ない（誤爆防止）。"""
        if not self.name_call_cfg["enabled"] or not self.name_call_cfg["aliases"]:
            return False
        if any(u.id in registered_bot_ids() for u in message.mentions):
            return False
        others = {str(a.get("home_channel_id")) for a in AGENTS
                  if a["id"] != self.agent["id"] and a.get("home_channel_id")}
        excluded = {str(x) for x in
                    (self.proactive_cfg.get("exclude_channel_ids") or ())}
        if not name_call_allowed_here(message.channel.id, others, excluded):
            return False
        text = message.clean_content or ""
        return any(a in text for a in self.name_call_cfg["aliases"])

    async def on_message(self, message):
        if message.guild is None or message.guild.id != GUILD_ID:
            return
        if self.is_archiver:
            # 同期DB書込はイベントループを止めないよう別スレッドで
            # （backfillと競合してもbusy_timeoutで待てる。順序はここでawaitして保つ）
            def _store(msg):
                with db.connect(DB_PATH) as conn:
                    archiving.store_message(conn, msg)
            await asyncio.to_thread(_store, message)
            # Webhook人格の応答（archiverが受信を代行。既存3体とは独立の経路）
            await self._maybe_webhook_persona(message)

        # 外部連携のプリフック（呼ばれなくても働くスキル）を先に通す。
        # True を返した連携があれば、このターンは処理済みとして通常応答しない
        if (self.integrations and not message.author.bot
                and message.author.id != (self.user.id if self.user else 0)):
            if await self._run_integration_prehooks(message):
                return

        trigger = self._trigger(message)
        if trigger is None:
            # 誰も呼ばれていない投稿でも、PDF添付だけは自動要約する
            await self._maybe_pdf_summary(message)
            return
        # リプライ先の添付は reference の有無で近似（フェッチせず軽量に判定。
        # 無言リプライ+メンションで「リプライ先の画像だけ」の依頼も通す）
        if not _content_gate(trigger, message.clean_content,
                             bool(message.attachments)
                             or bool(message.reference)):
            # 無言添付はノイズ防止で応答しない仕様のまま、PDFのみ例外
            await self._maybe_pdf_summary(message)
            return

        # YouTube URL → 要約スキル（人間の発言のみ・通常回答より先に判定）
        if self.youtube_summary and trigger in ("home", "human_mention"):
            if await self._maybe_youtube_summary(message):
                return

        history = await fetch_history(message)
        if trigger == "agent_mention":
            chain = trailing_bot_count(history) + 1  # +1 = トリガー発言自身
            if chain >= MAX_BOT_CHAIN:
                print(f"[{self.agent['id']}] chain limit reached "
                      f"({chain}/{MAX_BOT_CHAIN}) in #{message.channel}")
                return
        await self._respond(message, history)

    def _active_rules(self, message):
        """この文脈（全体＋このch＋この人）で有効な、自分のルール（期限切れ除外）。"""
        if message is None:
            return []
        scopes = rules.context_scopes(message.channel.id, message.author.id)
        now = reminders.fmt(reminders.now_jst())
        with db.connect(DB_PATH) as conn:
            return db.get_active_rules(conn, self.agent["id"], scopes, now=now)

    async def _reply_author_id(self, message):
        """リプライ先メッセージの著者id（無ければNone・best-effort）。
        訂正の学習（RM#17）で「自分の発言への返信か」を判定するのに使う。"""
        ref = message.reference
        if not ref or not ref.message_id:
            return None
        target = ref.resolved
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except Exception:
                return None
        if isinstance(target, discord.DeletedReferencedMessage):
            return None
        return getattr(getattr(target, "author", None), "id", None)

    def _build_agent_param(self, message=None, correction=False,
                           state_only=False):
        """search.answer_question に渡す agent dict（役割＋同僚一覧＋スキル）。
        correction=True は「自分の発言への訂正リプライ」検知時（RM#17）。"""
        # parts = system に入る安定した内容。会話ごとに変わるものは ctx_parts に
        # 分けて user プロンプト側へ渡す（毎回 system が変わると回答ごとに
        # 数万トークンのキャッシュ作り直しが起きる）
        parts = [self.agent.get("role") or "",
                 build_roster_note(self.agent["id"])]
        ctx_parts = []
        if self.image_gen:
            parts.append(IMAGE_SKILL_NOTE)
        if self.is_archiver and not message.author.bot:
            parts.append(wiki.WIKI_SKILL_NOTE)
        if self.youtube_summary:
            parts.append(YOUTUBE_SKILL_NOTE)
        if self.pdf_summary:
            parts.append(PDF_SKILL_NOTE)
        # 有効ルールは常に注入（これが「コード変更なしで新要求が効く」の要）
        active = self._active_rules(message)
        if active:
            parts.append(rules.build_rules_block(active))
        # 自己改善メモ（自己採点の週次蒸留・selfreview_distill.py）も常時注入
        with db.connect(DB_PATH) as conn:
            advice = db.recent_proactive_lessons(
                conn, self.agent["id"],
                limit=selfreview_distill.MAX_ADVICE, polarity="advice")
        if advice:
            parts.append(selfreview_distill.build_advice_block(advice))
        # ルール保存・能力起票・プラグインの指示文は人間の発言にのみ注入
        # （Bot同士の会話でマーカーを出す誘因自体を消す）
        if message is not None and not message.author.bot:
            tool_on = self._tool_loop_on()
            tool_live = self._tool_loop_live()
            if tool_on:
                parts.append(tool_launch.skill_note(tool_live))
            plugin_note = plugins.build_skill_notes(agent_runtime.PLUGINS)
            if plugin_note:
                parts.append(plugin_note)
            # 本番ツールループでは、ツールに置き換えたマーカー型スキルの指示文は
            # 出さない（マーカーとツールの二重経路を作らない）
            if not tool_live:
                parts.append(rules.build_skill_note(active))
                # 事実台帳スキル（2026-08-18）: 訂正・状況説明の受け皿
                with db.connect(DB_PATH) as conn:
                    recent_facts = db.search_facts(conn, [], limit=10)
                parts.append(facts.build_skill_note(recent_facts))
                parts.append(glossary.build_skill_note())
            terms_ctx = glossary.build_terms_context(
                glossary.load_terms(DB_PATH))
            if terms_ctx:
                parts.append(terms_ctx)
            # リマインダー指示文も人間発言にのみ（管理者は全員分を見られる）
            if self.reminder and not tool_live:
                is_admin = str(message.author.id) in ADMIN_IDS
                parts.append(reminders.build_skill_note(
                    reminders.now_jst(),
                    reminders.list_active(message.author.id),
                    all_entries=(reminders.list_active()
                                 if is_admin else None)))
            # 納期追跡の会話スキル: 追跡中一覧＋キャンセル/完了マーカー。
            # 「不要になった」への口約束だけで何も起きない事故の再発防止
            if self.action_tracking and not tool_live:
                with db.connect(DB_PATH) as conn:
                    open_items = db.open_action_items(conn, self.agent["id"])
                parts.append(action_items.build_skill_note(open_items))
            # 外部連携のスキル指示文（スプレッドシート・社内API等）。
            # 連携が無ければ何も足さない
            parts.extend(integrations.skill_notes(
                self.integrations, self._integration_ctx(message)))
            # 人物プロファイル（RM#1）: 発言者のプロファイルがあれば注入
            with db.connect(DB_PATH) as conn:
                prof = db.get_profile(conn, message.author.id)
            prof_block = profiles.build_profile_block(prof)
            if prof_block:
                ctx_parts.append(prof_block)
            # エピソード記憶（RM#3）＋得意分野マップ（RM#53）
            timeline = episodes.build_timeline_block(DB_PATH,
                                                     message.channel.id)
            if timeline:
                ctx_parts.append(timeline)
            if self.is_archiver:
                expertise = episodes.build_expertise_map(DB_PATH)
                if expertise:
                    ctx_parts.append(expertise)
            # 訂正の学習（RM#17）: 自分の発言への訂正リプライ検知時のみ
            if correction:
                ctx_parts.append(rules.build_correction_note(
                    state_only=state_only, tools=tool_live))
            # 自発発言の枠調整（Phase D）: マネージャ（アーカイブ担当）×管理者のみ告知
            if (self.is_archiver and str(message.author.id) in ADMIN_IDS
                    and not tool_live):
                enabled = [a["id"] for a in AGENTS
                           if (a.get("proactive") or {}).get("enabled")]
                if enabled:
                    parts.append(proactive.build_quota_skill_note(enabled))
        return {
            "name": self.agent["name"],
            "persona_files": self.persona_files,
            "role": "\n".join(p for p in parts if p),
            "context": "\n".join(p for p in ctx_parts if p),
        }

    def _integration_ctx(self, message, attachments_saved=None):
        """外部連携に渡す実行文脈を作る。

        Discord固有のオブジェクトは渡さない（integrations.Context の約束）。
        載せ替え先のプラットフォームでも同じ形になるよう、IDは文字列にする。
        """
        return integrations.Context(
            agent_id=self.agent["id"],
            agent_name=self.agent["name"],
            db_path=DB_PATH,
            config=agent_runtime.config,
            author_id=str(message.author.id),
            is_admin=str(message.author.id) in ADMIN_IDS,
            channel_id=str(message.channel.id),
            message_id=str(message.id),
            attachments=tuple(s["path"] for s in (attachments_saved or [])),
        )

    def _session_lock(self, channel_id, use_session):
        """セッション利用時だけch単位で直列化（非利用時は素通しの文脈）。"""
        if not use_session:
            return contextlib.nullcontext()
        lock = self._session_locks.get(channel_id)
        if lock is None:
            lock = self._session_locks[channel_id] = asyncio.Lock()
        return lock

    async def _respond(self, message, history):
        question = message.clean_content.strip()
        tmpdir = None
        target = message.channel   # 返信先（スレッド化した場合は差し替わる）
        try:
            atts = await _collect_attachments(message)
            supported, skipped = attachments.plan_attachments(atts)
            saved = []
            if supported:
                tmpdir, saved, failed = await attachments.download(supported)
                skipped = skipped + failed
            att_ctx = (attachments.build_context(tmpdir, saved, skipped)
                       if (saved or skipped) else None)
            if atts:
                print(f"[{self.agent['id']}] attachments: "
                      f"saved={len(saved)} skipped={len(skipped)}")
            # 検索から外す範囲の下限。【直近の会話】としてプロンプトに載る
            # ぶんだけを外し、同じchの**それより古いログは検索に残す**。
            # （ch丸ごと外すと、1ch運用ではアーカイブが全部消える）
            hist_ids = [h["id"] for h in (history or []) if h.get("id")]
            recent_from_id = min(hist_ids) if hist_ids else message.id
            # 発言＋直前の会話（キーワードゲートと事前注入の判定に使う）
            convo_tail = " ".join(
                (h.get("content") or "") for h in (history or [])[-5:])
            gate_text = f"{question} {convo_tail}"
            # 本文中の Discord メッセージリンク/ID を解決して参照ブロックにする
            references = await self._collect_reference_block(message)
            # 外部連携の現況スナップショット注入。ネットワークI/Oを含み得るので
            # 別スレッドで実行する。gate_text で関係ない時は連携側が None を返し、
            # API呼び出しゼロで済む
            extra_blocks = []
            if self.integrations and not message.author.bot:
                extra_blocks = await asyncio.to_thread(
                    integrations.context_blocks, self.integrations,
                    self._integration_ctx(message, saved), gate_text)
            # 訂正の学習（RM#17＋2026-08-18拡張）: 訂正語つきなら常に対象、
            # 訂正語なしの状況説明は「自分が直前に絡んでいる文脈」に限る
            # （事実台帳の受け皿ができたので取りこぼしを減らす）
            correction = state_only = False
            if not message.author.bot:
                if rules.looks_like_correction(question):
                    correction = True
                elif rules.looks_like_state_update(question):
                    ref_author = await self._reply_author_id(message)
                    mine = (self.user is not None
                            and ref_author == self.user.id)
                    if not mine and self.user is not None:
                        # リプライでなくても、直近履歴に自分の発言があれば
                        # 「さっきの話の訂正」の可能性が高い
                        mine = any(h.get("is_bot") for h in (history or [])[-4:])
                    correction = state_only = mine
            # runner_enabled なら invoke_claude 経由（v2 Phase 0）。
            # 同一契約なのでフラグを外すだけで旧経路に戻せる
            answer_fn = (runner_answer.answer_question if self.runner_enabled
                         else search.answer_question)
            # 会話セッションの持続（resume方式カナリア）: runner経路＋
            # 添付なしターンのみ。ch単位ロックで同一セッションの同時resumeを防ぐ
            sess_cfg = self.agent.get("session_resume") or {}
            tool_loop_on = self._tool_loop_on()
            # ツールループ中は resume しない: 引き継いだ会話では前ターンの
            # 「ツール無しで回答」の惰性でツールを使わない（告知を強めても 0 回）。
            # 直近の会話は history 注入で残るので文脈は保つ
            use_session = (agent_runtime.resolve_session_resume(
                               self.agent, self.runner_enabled)
                           and not tool_loop_on
                           and not (att_ctx is not None
                                    and att_ctx.has_supported))
            extra = {}
            if use_session:
                resume_sid = await asyncio.to_thread(
                    sessions.resume_id, DB_PATH, self.agent["id"],
                    message.channel.id, sess_cfg)
                extra = {"resume": resume_sid,
                         "session_cwd": sessions.SESSION_CWD}
            tool_plan = None
            if tool_loop_on:
                tool_plan = tool_launch.build(self._tool_context(message))
                extra.update(
                    mcp_config=tool_plan.mcp_config,
                    mcp_allow=tool_plan.allow,
                    max_budget_usd=self.tool_loop_cfg["max_budget_usd"],
                    on_event=self._tool_progress,
                    inject_search_hits=self.tool_loop_cfg["inject_search_hits"],
                    inject_facts=self.tool_loop_cfg["inject_facts"],
                    prompt_style=self.tool_loop_cfg["prompt_style"])
            async with self._session_lock(message.channel.id, use_session):
                async with ANSWER_SEM:
                    async with _best_effort_typing(message.channel):
                        try:
                            result = await asyncio.to_thread(
                                answer_fn, DB_PATH, str(GUILD_ID),
                                question, search.DEFAULT_MODEL,
                                message.channel.id,  # 応答中chは検索から除外
                                history,
                                self._build_agent_param(
                                    message, correction=correction,
                                    state_only=state_only),
                                attachments=att_ctx, references=references,
                                extra_blocks=extra_blocks,
                                recent_from_id=recent_from_id,
                                **extra,
                            )
                        except RuntimeError:
                            if not extra.get("resume"):
                                raise
                            # 壊れたセッションは破棄して新規で1回だけ再試行
                            print(f"[{self.agent['id']}] resume failed, "
                                  "retrying fresh")
                            await asyncio.to_thread(
                                sessions.clear, DB_PATH, self.agent["id"],
                                message.channel.id)
                            extra["resume"] = None
                            result = await asyncio.to_thread(
                                answer_fn, DB_PATH, str(GUILD_ID),
                                question, search.DEFAULT_MODEL,
                                message.channel.id,
                                history,
                                self._build_agent_param(
                                    message, correction=correction,
                                    state_only=state_only),
                                attachments=att_ctx, references=references,
                                extra_blocks=extra_blocks,
                                recent_from_id=recent_from_id,
                                **extra,
                            )
                if use_session and result.get("session_id"):
                    await asyncio.to_thread(
                        sessions.record_use, DB_PATH, self.agent["id"],
                        message.channel.id, result["session_id"],
                        resumed=bool(extra.get("resume")))
            answer = result["answer"]
            if tool_plan is not None:
                answer = self._apply_tool_evidence(
                    message, answer, result.get("events") or [])
                if self.tool_loop_cfg["shadow"]:
                    # シャドー観察用の一次証拠（実プロンプト・system・全イベント）。
                    # 「使えたのに使わなかった」を手元で再生して原因を追える
                    await asyncio.to_thread(
                        tool_launch.dump_trace, message.id,
                        {"question": question, "answer": answer,
                         "prompt": result.get("prompt"),
                         "system": result.get("system"),
                         "events": result.get("events") or [],
                         "meta": result.get("meta") or {},
                         "plan": tool_plan.mcp_config})
            image_prompt = caption = None
            if self.image_gen:
                answer, image_prompt, caption = extract_image_marker(answer)
            wiki_topics = []
            if self.is_archiver and not message.author.bot:
                answer, wiki_topics = wiki.extract_markers(answer)
            tool_live = (tool_plan is not None
                         and not self.tool_loop_cfg["shadow"])
            if self.reminder and not message.author.bot and not tool_live:
                answer = self._apply_reminder_markers(message, answer)
            # 外部連携のマーカー実行。ネットワークI/Oを含み得るので別スレッドで。
            # 実行は人間の発言のときだけ（Bot同士の会話で外部を書き換えない）
            if self.integrations and not message.author.bot:
                answer, notes = await asyncio.to_thread(
                    integrations.apply_all_markers, self.integrations,
                    self._integration_ctx(message), answer)
                for note in notes:
                    answer += f"\n{note}"
            # ルール保存・能力起票マーカーは人間の発言にのみ適用
            if not message.author.bot:
                if tool_live:
                    # 本番ツールループ: 置き換え済みマーカーは除去だけ
                    # （実行はツールが済ませ、証拠行も付いている）
                    answer = tool_launch.strip_retired_markers(answer)
                else:
                    if self.action_tracking:
                        answer = self._apply_action_markers(message, answer)
                    answer = self._apply_rule_markers(message, answer)
                    answer = self._apply_fact_markers(message, answer)
                    answer = self._apply_quota_markers(message, answer)
                    answer = self._apply_glossary_markers(message, answer)
                # 「できたフリ」検出（RM#20）: 完了主張×マーカー不発を正直化。
                # ツールで引いたヒット数も根拠として数える（注入を減らすと
                # 事前ヒットが0になるため）
                hits = result.get("hits", 0)
                tools_used = None
                if tool_plan is not None:
                    events = result.get("events") or []
                    hits = max(hits, tool_evidence.search_hits(events))
                    if tool_live:
                        tools_used = tool_evidence.tools_used(events)
                answer = self._apply_honesty_check(message, answer, hits,
                                                   tools_used=tools_used)
            # 単語帳（RM#5）: 誤表記を決定論で常時修正（登録直後の回答から効く）
            pairs = glossary.load_pairs(DB_PATH)
            if pairs:
                answer = glossary.apply(answer, pairs)
            # プラグインマーカーは常に除去（生マーカーをchに晒さない）。
            # 実行結果の反映は人間発言のみ（Bot間会話でのツール発火を防ぐ）
            if agent_runtime.PLUGINS:
                used_markers = [mk for mk in agent_runtime.PLUGINS
                                if mk in answer]
                answer, plugin_out = await asyncio.to_thread(
                    plugins.apply_markers, agent_runtime.PLUGINS, answer)
                if plugin_out and not message.author.bot:
                    answer = (answer + "\n" if answer else "") \
                        + "\n".join(plugin_out)
                    if used_markers:
                        # スキル使用統計（RM#23）: 月次棚卸しの原料
                        await asyncio.to_thread(
                            proactive.log_entry, DB_PATH, self.agent["id"],
                            kind="plugin", action="used",
                            channel_id=message.channel.id,
                            detail=",".join(used_markers)[:100])
            if answer:
                target = await agent_runtime.deliver_reply(
                    message, answer, cfg=self.thread_reply_cfg,
                    kind="mention", agent_id=self.agent["id"])
                # 投稿セルフレビュー（RM#14）: 投稿後に採点だけ記録（シャドー・
                # レイテンシに乗せない・失敗しても本流に影響なし）
                if (self.self_review_cfg.get("enabled")
                        and not message.author.bot
                        and len(answer) >= self_review.MIN_ANSWER_LEN):
                    task = asyncio.create_task(
                        self._self_review_bg(
                            question, answer, message,
                            events=(result.get("events") or []
                                    if tool_plan is not None else [])))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
            for topic in wiki_topics[:2]:
                await self._create_wiki_page(message, topic)
            if image_prompt:
                posted_img = await self._generate_image(
                    target, image_prompt, caption,
                    collect_ref_image_paths(saved))
                # 相互レビュー（RM#59）: 納品物に同僚の観点を一言添えてもらう
                if posted_img is not None and self.agent.get("peer_review"):
                    task = asyncio.create_task(
                        self._request_peer_review(message, image_prompt))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
            if self.runner_enabled:
                # 文脈要約を非同期更新（差分が小さいうちはスキップされる）。
                # 参照を保持しないとタスクがGC回収され得るため _bg_tasks で持つ
                task = asyncio.create_task(
                    self._update_thread_summary(message.channel.id))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        except Exception as e:
            print(f"[{self.agent['id']}] answer failed: {e}")
            await message.channel.send(
                f"⚠️ 回答生成に失敗しました: {str(e)[:300]}")
        finally:
            # 添付の一時dirは画像生成（参考画像に使用）まで終わってから消す
            attachments.cleanup(tmpdir)

    async def _create_wiki_page(self, message, topic):
        """Wikiページの編纂・投稿・登録（#103）。既存トピックなら同じ投稿を
        編集で更新する。"""
        try:
            def _existing():
                with db.connect(DB_PATH) as conn:
                    return {p["topic"]: p for p in db.wiki_pages_all(conn)}
            pages = await asyncio.to_thread(_existing)
            async with ANSWER_SEM:
                body = await asyncio.to_thread(
                    wiki.compile_page, DB_PATH, topic, GUILD_ID,
                    model=search.DEFAULT_MODEL)
            if not body:
                await message.channel.send(
                    f"-# 📖 「{topic}」はまだ記録が少なくてページにできな"
                    "りませんでした（決定が溜まったら作れます）",
                    allowed_mentions=ALLOWED_MENTIONS)
                return
            page = pages.get(topic)
            posted = None
            if page:
                try:
                    channel = (self.get_channel(page["channel_id"])
                               or await self.fetch_channel(page["channel_id"]))
                    posted = await channel.fetch_message(page["message_id"])
                    await posted.edit(content=body[:1990])
                except Exception:
                    posted = None
            if posted is None:
                posted = await message.channel.send(
                    body[:1990],
                    allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(
                wiki.save_page, DB_PATH, topic=topic,
                channel_id=posted.channel.id, message_id=posted.id,
                created_by=message.author.id)
            await message.channel.send(
                f"-# 📖 「{topic}」のWikiページ{'を更新した' if page else 'を作った'}"
                "です。以後、関連する決定が入るたび自動で更新し続けます"
                "（ピン留め推奨です）",
                allowed_mentions=ALLOWED_MENTIONS)
        except Exception as e:
            print(f"[{self.agent['id']}] wiki create failed ({topic}): {e}")

    async def _request_peer_review(self, message, what):
        """同僚（設定 peer_review のid）にレビューを依頼する（RM#59）。
        メンションするだけ＝相手の通常フローが自分の専門観点で返す。"""
        try:
            peer_id = self.agent.get("peer_review")
            uid = AGENT_USER_IDS.get(peer_id)
            peer = next((a for a in AGENTS if a["id"] == peer_id), None)
            if not uid or peer is None:
                return
            await asyncio.sleep(3)   # 画像投稿が流れてから声をかける
            # role は複数行の自己紹介文なので、先頭の1文だけを肩書きとして使う
            # （固定長で切ると文の途中で切れて意味が崩れる）
            role = (peer.get("role") or "専門").split("。")[0][:30]
            await message.channel.send(
                f"<@{uid}> いまの「{what[:40]}」について、"
                f"{role}の観点で気になるところはありますか？"
                "（一言で大丈夫です）",
                allowed_mentions=discord.AllowedMentions(
                    users=True, everyone=False, roles=False))
        except Exception as e:
            print(f"[{self.agent['id']}] peer review request failed: {e}")

    async def _self_review_bg(self, question, answer, message, events=None):
        """投稿後の自己採点（RM#14・シャドー）。結果はproactive_logへ記録のみ。
        events: ツールループの stream-json。使ったツールと結果を根拠として渡す
        （検索して答えた事実を「根拠のない断定」と誤採点しないため）。"""
        try:
            evidence = tool_evidence.summarize_for_review(events or [])
            async with ANSWER_SEM:
                r = await asyncio.to_thread(
                    self_review.review, question, answer,
                    model=self.proactive_cfg.get(
                        "screen_model", proactive.SCREEN_MODEL_DEFAULT),
                    evidence=evidence or None)
            if r:
                await asyncio.to_thread(
                    proactive.log_entry, DB_PATH, self.agent["id"],
                    kind="selfreview", action="score",
                    channel_id=message.channel.id,
                    trigger_message_id=message.id,
                    detail=f"{r['score']}|{r['issue']}")
        except Exception as e:
            print(f"[{self.agent['id']}] self review failed: {e}")

    async def _collect_reference_block(self, message):
        """本文中の Discord メッセージリンク/ID と**リプライ先**を解決して参照
        ブロックを返す。まず アーカイブDB（全会話を保存済み）から引き、無い分だけ
        Discord から best-effort で取得する。1件も参照が無ければ None。

        リプライ先が入っていないと、通知や長文にリプライして「これ対応して」と
        頼まれても何の話か分からず聞き返してしまう。リプライは「この投稿の話」
        という最も強い文脈なので先頭に置く。"""
        refs = msgref.extract_refs(message.clean_content or "", GUILD_ID)
        ref = message.reference
        if ref is not None and ref.message_id:
            head = (ref.channel_id or message.channel.id, ref.message_id)
            refs = [head] + [r for r in refs if r[1] != head[1]]
            refs = refs[:msgref.MAX_REFS]
        if not refs:
            return None
        with db.connect(DB_PATH) as conn:
            resolved = msgref.resolve_from_db(conn, refs)
        entries = []
        for channel_id, mid in refs:
            entry = resolved.get(mid)
            if entry is None:
                entry = await self._fetch_ref_message(channel_id, mid, message)
            if entry is not None:
                entries.append(entry)
        return msgref.build_reference_block(entries, GUILD_ID) or None

    async def _fetch_ref_message(self, channel_id, message_id, origin):
        """DBに無い参照を Discord から取得（best-effort）。他guildは参照しない。
        素のID（channel不明）は発言chから探す。取得不能なら None。"""
        try:
            if channel_id:
                channel = (self.get_channel(channel_id)
                           or await self.fetch_channel(channel_id))
            else:
                channel = origin.channel
            target = await channel.fetch_message(message_id)
        except Exception:
            return None
        if target.guild is None or target.guild.id != GUILD_ID:
            return None
        created = target.created_at.isoformat() if target.created_at else None
        return msgref.make_entry(
            message_id=target.id, channel_id=target.channel.id,
            channel=getattr(target.channel, "name", None),
            author=target.author.display_name, author_id=target.author.id,
            content=target.clean_content or target.content or "",
            created_at=created)

    async def _update_thread_summary(self, channel_id):
        """応答後の文脈要約更新（best-effort・失敗しても応答には影響しない）。"""
        try:
            # 直前の自分の回答がアーカイブに載るのを少し待つ
            await asyncio.sleep(5)
            lock = SUMMARY_LOCKS.setdefault(channel_id, asyncio.Lock())
            async with lock:  # 同一chの並行更新を直列化（全エージェント共有）
                async with ANSWER_SEM:
                    updated = await asyncio.to_thread(
                        summaries.maybe_update, DB_PATH, channel_id)
            if updated:
                print(f"[{self.agent['id']}] thread summary updated "
                      f"(ch={channel_id}, {len(updated)} chars)")
        except Exception as e:
            print(f"[{self.agent['id']}] summary update failed: {e}")

    async def _generate_image(self, channel, prompt, caption=None,
                              ref_paths=None):
        imagegen = load_imagegen(self.image_gen)
        if imagegen is None:
            # 画像生成の連携が入っていない（既定はこちら）。黙って諦めず理由を出す
            await channel.send(
                "⚠️ 画像生成の連携が設定されていません"
                "（docs/07-integrations.md を参照してください）")
            return None
        ref_note = f"参考画像{len(ref_paths)}枚付き、" if ref_paths else ""
        notice = await channel.send(
            f"🎨 画像を生成中です…（{ref_note}数分かかることがあります）")
        try:
            async with IMAGE_SEM:
                path = await asyncio.to_thread(
                    imagegen.generate, prompt, self.image_gen,
                    ref_paths or None)
            # 無言で画像だけ投稿しない: キャプション（デザイン担当が回答時に用意）を添える
            return await channel.send(content=caption or "できました！",
                                      file=discord.File(path),
                                      allowed_mentions=ALLOWED_MENTIONS)
        except Exception as e:
            print(f"[{self.agent['id']}] image generation failed: {e}")
            await channel.send(f"⚠️ 画像生成に失敗しました: {str(e)[:300]}")
            return None
        finally:
            try:
                await notice.delete()
            except Exception:
                pass

    async def on_message_edit(self, before, after):
        if not self.is_archiver:
            return
        if after.guild is None or after.guild.id != GUILD_ID:
            return
        with db.connect(DB_PATH) as conn:
            db.update_content(
                conn,
                id=after.id,
                content=after.content,
                edited_at=after.edited_at.isoformat() if after.edited_at else None,
            )

    async def on_message_delete(self, message):
        if not self.is_archiver:
            return
        if message.guild is None or message.guild.id != GUILD_ID:
            return
        with db.connect(DB_PATH) as conn:
            db.mark_deleted(conn, message.id)

def _record_llm_call(meta):
    """core/invoke_claude の計測フック（v4 Phase 0）: 1起動1行を llm_calls へ。"""
    with db.connect(DB_PATH) as conn:
        db.add_llm_call(conn, created_at=reminders.fmt(reminders.now_jst()),
                        **meta)


async def _run_client(client):
    """クライアントごとのタスク起点。contextvar に自分のidを置くと、
    この配下で作られる全タスク／to_thread に「どのエージェントの起動か」が届く。"""
    invoke_claude.CURRENT_AGENT.set(client.agent["id"])
    await client.start(client.agent["token"])


async def main():
    # 設定が揃っていなければ、何が足りないかを並べて終了する
    # （黙って起動して「反応しないBOT」になるのが一番わかりにくい）
    problems = agent_runtime.validate_config(agent_runtime.config)
    if problems:
        print("設定が足りないため起動できません:")
        for p in problems:
            print(f"  - {p}")
        print("\nダッシュボード（python start.py）から設定してください。")
        sys.exit(1)

    # 繋ぎ先だけ別サーバーに変えられていないか。記録には guild_id が無く、
    # 前のサーバーの会話を選んで消せないので、混ざる前にここで止める
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        stored = db.archived_guild_id(conn)
    mismatch = app_config.guild_mismatch_problem(stored, GUILD_ID)
    if mismatch:
        print("起動できません:")
        print(f"  - {mismatch}")
        sys.exit(1)

    # 過去障害対策: discordロガーのINFO spam（RESUMED等）でログ肥大させない
    discord.utils.setup_logging(level=logging.WARNING)
    # LLM計測フック（v4 Phase 0）。import 時ではなく起動時に登録する
    # （テストが bot を import しても本番DBへ書かないため）
    invoke_claude.RECORDER = _record_llm_call

    clients = []
    for agent in AGENTS:
        if not agent.get("token"):
            print(f"[{agent['id']}] token未設定のためスキップ")
            continue
        clients.append(AgentClient(agent, intents=intents))

    try:
        await asyncio.gather(*(_run_client(c) for c in clients))
    finally:
        # 1体でも落ちたら全員閉じてプロセス終了 → launchdが再起動
        await asyncio.gather(*(c.close() for c in clients),
                             return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
