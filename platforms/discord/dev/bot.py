#!/usr/bin/env python3
"""開発BOT（AI開発室 専属・プロセス監視兼務）— Phase 1: 監視ハット。

別プロセス・別Botアカウントで常駐し、archivebot / meetingbot の健全性を監視して
AI開発室chに異常/復帰を通知する。判定は monitor.py（純粋関数）に委譲。

主信号は「Discord上でオフライン表示か(presence)」＝人間の見え方と一致。
アーカイブ担当はmeetingbotと同アカウント共有のため、archivebot固有判定にはデザイン担当/マーケ担当を使う
（config.dev_bot.monitor.targets[].presence_bot_names）。

Phase 2以降で開発指示の受け口・worktree実装・承認デプロイをこの同じBOTに足す。
起動: launchd（com.discord.devbot）。手動: ./venv は chatbot のものを流用。
"""

import asyncio
import datetime
import io
import json
import logging
import os
import re
import subprocess
import sys
import time

import discord

from platforms.discord.dev import monitor
from platforms.discord.dev import dev_pipeline
from platforms.discord.dev import dev_report
from platforms.discord.dev import deploy
from platforms.discord.dev import persona
from platforms.discord.dev import roadmap
from platforms.discord.dev import router

from core import db
from core import heartbeat
from core import invoke_claude
from core import paths

HERE = os.path.dirname(os.path.abspath(__file__))
# 設定・DBはリポジトリ共通のものを見る（tokenは dev_bot.token に貼る）
CONFIG_PATH = paths.CONFIG_PATH
DB_PATH = paths.DB_PATH
PERSONA_PATH = os.path.join(paths.PERSONAS_DIR, "devbot.md")
# スーパーバイザが鮮度を見る生存証明
# （プロセス生存でなく監視ループの健全性を示す）
HEARTBEAT_PATH = os.path.join(paths.HEARTBEAT_DIR, "devbot")
TOKEN_PLACEHOLDER = "PASTE_DEV_BOT_TOKEN_HERE"


def _load_persona():
    try:
        with open(PERSONA_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# メンション会話でツール呼び出しが本文に漏れた時の検知（Bash: {command:...} 等）。
# claude -p は --tools "" でも調査質問だとツールコールをテキストに書き出すことがある。
_TOOL_LEAK_RE = re.compile(r'(?:^|\n)\s*(?:Bash|Grep|Glob|Read|Write|Edit)\s*:'
                           r'|"command"\s*:|<tool_use')


def _sanitize_reply(text):
    """会話返信の安全網。ツールコール漏れ/空なら安全な定型に差し替える（純粋関数）。"""
    t = (text or "").strip()
    if not t or _TOOL_LEAK_RE.search(t):
        return "うーん、うまく確認できませんでした💦 いまの様子は `!status` で見れますよ！"
    return t


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_dev_config(path=CONFIG_PATH):
    """config.json から guild_id・admins・dev_bot を取り出す。未設定は明快に落とす。"""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    dev = cfg.get("dev_bot")
    if not dev:
        sys.exit("config.json に dev_bot セクションがありません")
    admins = {str(x) for x in cfg.get("admins", [])}
    return int(cfg["guild_id"]), admins, dev


class DevBot(discord.Client):
    """監視ハット。interval毎に各ターゲットを観測→状態遷移時のみ通知。"""

    def __init__(self, *, guild_id, admins, dev_cfg, **kwargs):
        super().__init__(**kwargs)
        self.guild_id = guild_id
        self.admins = admins            # 開発指示を出せるのは管理者のみ
        self.dev_cfg = dev_cfg
        self.dev_channel_id = int(dev_cfg["dev_channel_id"])
        mon = dev_cfg.get("monitor") or {}
        self.interval_sec = int(mon.get("interval_sec", 60))
        self.stall_after_sec = int(mon.get("stall_after_sec",
                                            monitor.DEFAULT_STALL_AFTER_SEC))
        self.targets = mon.get("targets", [])
        self._prev = {}                 # target名 -> 確定済みstatus（遷移検出用）
        self._pending = {}              # target名 -> (確定待ちstatus, 連続回数)
        self._monitor_task = None
        self._running_jobs = set()      # 実装中の起票id（二重起動防止）
        self._bg = set()                # 実行中タスクの強参照（GC回収防止）
        self._lesson_prompts = {}       # 理由聞きmsg_id -> (cap_req_id, job_id)
        self._active_job_ids = {}       # 実装中 req_id -> job_id（例外時のfailed化用）
        self._summary_msg_ids = []      # 直近の!roadmap要約msg_id（要約への👍👎対応）

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def on_ready(self):
        print(f"開発BOT logged in as {self.user}")
        await asyncio.to_thread(db.init_db, DB_PATH)   # dev_jobs テーブル等を確保
        channel = self.get_channel(self.dev_channel_id)
        if channel is None:
            print(f"dev_channel {self.dev_channel_id} not found（招待/権限を確認）")
        # 起動時診断（1回・非spam）: guild解決とpresence取得が効いているか可視化
        guild = self.get_guild(self.guild_id)
        print(f"[診断] guild={guild.name if guild else None} "
              f"members={len(guild.members) if guild else 0}")
        for target in self.targets:
            sig = await self._gather(target)
            age = int(sig.log_age_sec) if sig.log_age_sec is not None else None
            print(f"[診断] {target['name']}: alive={sig.process_alive} "
                  f"online={sig.discord_online} log_age={age}s "
                  f"exit={sig.last_exit_status}")
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            names = ", ".join(t["name"] for t in self.targets)
            await self._notify(persona.startup(names, self.interval_sec))
            await self._recover_interrupted_jobs()
            # 進化ロードマップ: シード投入（冪等）→未提案があればカードを1枚出す
            try:
                added = await asyncio.to_thread(roadmap.seed, DB_PATH)
                if added:
                    print(f"roadmap seeded: {added}件")
                await self._maybe_post_card()
            except Exception as e:
                print(f"roadmap init failed: {e}")

    async def _recover_interrupted_jobs(self):
        """再起動で中断された building ジョブを interrupted にして正直に報告する
        （残った status=building は必ず孤児＝実行中プロセスは前世代と共に死んでいる）。"""
        def _mark():
            with db.connect(DB_PATH) as conn:
                jobs = db.list_dev_jobs_by_status(conn, "building")
                for j in jobs:
                    db.update_dev_job(conn, j["id"], updated_at=_now_iso(),
                                      status="interrupted")
                return jobs
        for j in await asyncio.to_thread(_mark):
            await self._notify(persona.job_interrupted(j["cap_req_id"]))

    # --- 監視ループ --------------------------------------------------------
    async def _monitor_loop(self):
        await self.wait_until_ready()
        tick = 0
        while not self.is_closed():
            try:
                await self._check_all()
            except Exception as e:                       # 1回の失敗でループを殺さない
                print(f"monitor loop error: {e}")
            tick += 1
            if tick % 15 == 0:      # 約15分ごと: カナリア監視＋起票の拾い上げ
                try:
                    await self._canary_check()
                except Exception as e:
                    print(f"canary check error: {e}")
                try:
                    await self._maybe_propose_cap()
                except Exception as e:
                    print(f"cap watch error: {e}")
                try:
                    await self._maybe_dev_report()
                except Exception as e:
                    print(f"dev report error: {e}")
            # ループが回っている証明（IMAPハング型の「生きてるが止まる」を
            # bot-watchdog が外から検知できるようにする）。ゲートウェイから
            # 切れている間は書かない＝オフライン固着もwatchdogが拾える
            # （latencyのnan比較: nan==nan は False）
            if self.is_ready() and self.latency == self.latency:
                await asyncio.to_thread(self._touch_heartbeat)
            await asyncio.sleep(self.interval_sec)

    @staticmethod
    def _touch_heartbeat():
        """生存証明（core/heartbeat.py が読める形で書く）。"""
        heartbeat.touch("devbot")

    async def _check_all(self, *, force=False):
        """全ターゲットを観測して分類。force=Trueなら遷移に関係なく現況を返す。"""
        lines = []
        for target in self.targets:
            sig = await self._gather(target)
            # ログ鮮度による無音検知はイベント駆動ログ（会議中だけ書く等）では
            # 誤検知するため opt-in（stall_check:true のターゲットのみ）。
            # off時は閾値を実質無限大にして classify を停滞させない。
            stall = (target.get("stall_after_sec", self.stall_after_sec)
                     if target.get("stall_check") else 10 ** 9)
            health = monitor.classify(sig, stall_after_sec=stall)
            name = target["name"]
            lines.append(monitor.format_notice(name, health))
            if force:
                # !status は表示のみ＝状態機械に一切触れない。ここで_prevや
                # デバウンスを進めると、後続の監視ループが本物の遷移通知を
                # 「通知済み」と誤認して握り潰す
                continue
            # デバウンス: 同じ観測がCONFIRM_AFTER回続くまで遷移を確定しない
            # （瞬断の🟡/✅往復spam対策。DOWNは即確定）
            cand, streak = self._pending.get(name, (None, 0))
            cand, streak, confirmed = monitor.confirm(cand, streak,
                                                      health.status)
            self._pending[name] = (cand, streak)
            if confirmed is not None:
                if monitor.should_notify(self._prev.get(name), confirmed):
                    # 状態遷移のプッシュはつむぎの声で
                    await self._notify(persona.alert(name, confirmed))
                self._prev[name] = confirmed
        return lines

    async def _gather(self, target):
        """1ターゲットの観測。ブロッキングIO(subprocess)は別スレッドへ逃がす。"""
        label = target["launchd_label"]
        log_path = os.path.join(HERE, target["log_path"])

        def _probe():
            pid, exit_st = monitor.launchctl_probe(label)
            age = monitor.log_age_sec(log_path, now=time.time())
            return pid, exit_st, age

        pid, exit_st, age = await asyncio.to_thread(_probe)
        online = self._presence_online(target.get("presence_bot_names", []))
        return monitor.Signals(process_alive=pid is not None,
                               discord_online=online, log_age_sec=age,
                               last_exit_status=exit_st)

    def _presence_online(self, names):
        """presence_bot_names のいずれかがオンライン表示か。
        名前が1つも解決できなければ None（不明＝警報しない）。空指定も None。"""
        if not names:
            return None
        statuses = [m.status for m in self._guild_members()
                    if m.name in names or m.display_name in names]
        if not statuses:
            return None
        # 同一プロセスの複数アカウントは同時に落ちる → 1つでもオンラインなら生存
        return any(s is not discord.Status.offline for s in statuses)

    def _guild_members(self):
        guild = self.get_guild(self.guild_id)
        return guild.members if guild else []

    async def _notify(self, text):
        """AI開発室へ通知。送れたら Message を返す（呼び出し側は通常無視でよい）。"""
        print(f"[通知] {text}")               # 監査/実証用（送信内容を必ずログに残す）
        channel = self.get_channel(self.dev_channel_id)
        if channel is None:
            print(f"[notify不可] {text}")
            return None
        try:
            return await channel.send(text)
        except discord.DiscordException as e:
            print(f"notify送信失敗: {e}")
            return None

    # --- 手動コマンド（管理者・AI開発室のみ） -----------------------------
    async def on_message(self, message):
        if (message.guild is None or message.channel.id != self.dev_channel_id
                or message.author.bot):
            return
        content = (message.content or "").strip()
        # 👎後の「理由聞き」への返信は教訓として保存（他のハンドラより先に確定させる）
        ref_id = message.reference.message_id if message.reference else None
        if ref_id in self._lesson_prompts:
            if str(message.author.id) in self.admins and content:
                req_id, job_id = self._lesson_prompts.pop(ref_id)
                await asyncio.to_thread(self._add_lesson, req_id, job_id,
                                        "rejected", content[:500])
                await message.reply(persona.lesson_saved(),
                                    mention_author=False)
            return
        mentioned = self._is_mentioned(message)
        if content in ("!status", "!監視"):
            async with message.channel.typing():
                lines = await self._check_all(force=True)
                dirty = await asyncio.to_thread(deploy.dirty_files)
            if dirty:
                lines.append(f"⚠️ mainに未コミット変更 {len(dirty)}件"
                             "（デプロイ対象と重なるとmergeできないので、早めのコミット推奨です）")
            await self._notify(persona.status_header() + "\n" + "\n".join(lines))
            return
        if content == "!ping":
            await self._notify(persona.ping())
            return
        if content == "!roadmap":
            text = await asyncio.to_thread(
                roadmap.progress_summary, DB_PATH, self.guild_id,
                self.dev_channel_id)
            msg = await self._notify(text)
            if msg is not None:
                # 要約への👍👎も「いま提案中のカード」への判断として受ける
                self._summary_msg_ids.append(msg.id)
                del self._summary_msg_ids[:-5]   # 直近5件だけ覚える
            return
        if content == "!card":
            posted = await self._maybe_post_card()
            if not posted:
                await self._notify("いま提案中のカードがあるか、もう全部"
                                   "出し切ってます！（`!roadmap` で確認してくださいね）")
            return
        m = re.fullmatch(r"!revert\s+#?(\d+)", content)
        if m:
            if str(message.author.id) not in self.admins:
                await self._notify(persona.not_admin())
                return
            self._spawn(self._revert_flow(int(m.group(1))))
            return
        req_id = dev_pipeline.parse_dev_command(content)
        if req_id is not None:
            if str(message.author.id) not in self.admins:
                await self._notify(persona.not_admin())
                return
            self._spawn(self._run_dev_job(
                req_id, fresh=dev_pipeline.wants_fresh_start(content)))
            return
        # それ以外でメンションされたら、起票リストと照合してルーティングする
        if mentioned:
            self._spawn(self._handle_mention(message))

    def _is_mentioned(self, message):
        """ユーザーメンション or Bot自身のロールメンションを検知。
        「@開発BOT」がBot同名の管理ロール(<@&…>)に解決される場合に対応する。"""
        if self.user and self.user in message.mentions:
            return True
        if message.role_mentions and self.user:
            guild = self.get_guild(self.guild_id)
            me = guild.get_member(self.user.id) if guild else None
            if me is not None:
                my_roles = set(me.roles)
                return any(r in my_roles for r in message.role_mentions)
        return False

    async def _handle_mention(self, message):
        """メンションを解釈: 該当起票が明確なら着手、曖昧なら聞き返し、他は会話。"""
        admin = str(message.author.id) in self.admins
        try:
            async with message.channel.typing():
                d = await asyncio.to_thread(
                    self._route_mention, message.clean_content)
            if d["action"] == "start":
                if not admin:
                    await message.reply(persona.not_admin(), mention_author=False)
                elif d["req_id"] in self._running_jobs:
                    await message.reply(persona.already_running(d["req_id"]),
                                        mention_author=False)
                else:
                    self._spawn(self._run_dev_job(   # job_startを自ら投稿
                        d["req_id"],
                        fresh=dev_pipeline.wants_fresh_start(
                            message.clean_content)))
            else:
                await message.reply(
                    d["reply"] or "…えっと、もう一回言ってもらえますか？",
                    mention_author=False)
        except discord.DiscordException as e:
            print(f"mention handling error: {e}")

    @staticmethod
    def _chat_context():
        """会話に渡す実データ（open起票＋最近のdev_jobs）。これで“調べに行く”動機を消す。"""
        try:
            with db.connect(DB_PATH) as conn:
                caps = conn.execute(
                    "SELECT id, substr(description,1,60) FROM capability_requests"
                    " WHERE status='open' ORDER BY id").fetchall()
                jobs = conn.execute(
                    "SELECT id, cap_req_id, status FROM dev_jobs"
                    " ORDER BY id DESC LIMIT 5").fetchall()
        except Exception:
            return "（状況データ取得失敗）"
        lines = ["未対応の起票(status=open):"]
        lines += [f"  #{c[0]}: {c[1]}" for c in caps] or ["  なし"]
        lines.append("最近の実装ジョブ(dev_jobs):")
        lines += [f"  job#{j[0]} 起票#{j[1]} = {j[2]}" for j in jobs] \
            or ["  まだ1件も動かしていない"]
        return "\n".join(lines)

    @staticmethod
    def _open_caps():
        """未対応の起票（照合＋start許可の対象）。"""
        try:
            with db.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT id, substr(description,1,80) FROM capability_requests"
                    " WHERE status='open' ORDER BY id").fetchall()
        except Exception:
            return []
        return [{"id": r[0], "desc": r[1]} for r in rows]

    @staticmethod
    def _route_mention(user_text):
        """発言→{action, req_id, reply}。start は実在の open 起票のときだけ許可。"""
        caps = DevBot._open_caps()
        system = router.build_route_system(
            _load_persona(), DevBot._chat_context(), caps)
        try:
            res = invoke_claude.invoke(user_text or "（無言でメンションされた）",
                                       model="claude-sonnet-5", system=system)
            d = router.parse_route(res.text, {c["id"] for c in caps})
        except Exception as e:
            return {"action": "chat", "req_id": None,
                    "reply": f"ごめんなさい、いま返せないです💦（{e}）"}
        # ツールコール漏れの安全網（reply が漏れていたら空にしてフォールバックさせる）
        if d["reply"] and _TOOL_LEAK_RE.search(d["reply"]):
            d["reply"] = ""
        return d

    # --- 開発パイプライン（Phase 2: 実装→検証→承認待ちで停止） --------------
    def _channel(self):
        return self.get_channel(self.dev_channel_id)

    async def _safe_edit(self, msg, content):
        try:
            await msg.edit(content=content[:1900])
        except discord.DiscordException as e:
            print(f"進捗edit失敗: {e}")

    async def _run_dev_job(self, req_id, *, fresh=False):
        """起票1件を worktree で実装→検証→サマリー投稿（👍手前で停止）。
        fresh=True は前回の途中経過を破棄してゼロから（既定は続きから再開）。"""
        if req_id in self._running_jobs:
            await self._notify(persona.already_running(req_id))
            return
        cap_req = await asyncio.to_thread(self._load_cap_req, req_id)
        if cap_req is None:
            await self._notify(persona.job_not_found(req_id))
            return
        channel = self._channel()
        if channel is None:
            print("dev_channel未解決のため開発ジョブ中止")
            return
        self._running_jobs.add(req_id)
        # 同じ起票の古い承認待ち(built)は無効化する。worktree/ブランチ名は起票idで
        # 共有のため、古いサマリーへの👍が新ジョブの成果物をmergeしてしまう（承認
        # すり替え）のを防ぐ
        await asyncio.to_thread(self._supersede_built, req_id)
        prog = await channel.send(
            persona.job_start(req_id, cap_req["description"]))
        try:
            await self._execute_pipeline(req_id, cap_req, prog, fresh=fresh)
        except Exception as e:
            print(f"dev job error: {e}")
            # buildingのまま残すと次回の再実行がcreate_worktreeで成果を破壊する
            jid = self._active_job_ids.pop(req_id, None)
            if jid is not None:
                await asyncio.to_thread(self._set_job_status, jid, "failed")
            await self._safe_edit(prog, persona.job_error(req_id, e))
        finally:
            self._running_jobs.discard(req_id)

    @staticmethod
    def _load_cap_req(req_id):
        with db.connect(DB_PATH) as conn:
            return db.get_capability_request(conn, req_id)

    @staticmethod
    def _supersede_built(req_id):
        with db.connect(DB_PATH) as conn:
            db.supersede_built_jobs(conn, req_id, updated_at=_now_iso())

    @staticmethod
    def _claim_job(job_id, from_status, to_status):
        with db.connect(DB_PATH) as conn:
            return db.claim_dev_job(conn, job_id, from_status=from_status,
                                    to_status=to_status, updated_at=_now_iso())

    @staticmethod
    def _resumable(req_id):
        """前回ジョブがfailed/interruptedでworktreeが生きていれば続きから再開できる。"""
        with db.connect(DB_PATH) as conn:
            prev = db.latest_dev_job_for_cap(conn, req_id)
        if prev is None or prev["status"] not in ("failed", "interrupted"):
            return False
        _branch, wt, _cwd, _archive = dev_pipeline.worktree_paths(req_id)
        return dev_pipeline.worktree_usable(wt)

    @staticmethod
    def _prompt_inputs():
        """規約（データファイル）と直近の教訓（DB）を読む（別スレッド用）。"""
        guidelines = dev_pipeline.load_guidelines()
        with db.connect(DB_PATH) as conn:
            lessons = db.recent_dev_lessons(conn)
        return guidelines, lessons

    @staticmethod
    def _add_lesson(cap_req_id, job_id, kind, text):
        with db.connect(DB_PATH) as conn:
            db.add_dev_lesson(conn, cap_req_id=cap_req_id, job_id=job_id,
                              kind=kind, text=text, created_at=_now_iso())

    async def _execute_pipeline(self, req_id, cap_req, prog, *, fresh=False):
        progress = dev_pipeline.ProgressBuffer()
        # 1) worktree（前回failed/interruptedの残骸が使えるなら続きから＝成果を捨てない）
        progress.set_phase("worktree準備中")
        resume = False
        if not fresh:
            resume = await asyncio.to_thread(self._resumable, req_id)
        if resume:
            branch, wt, cwd, archive_dir = dev_pipeline.worktree_paths(req_id)
            await self._notify(persona.resuming(req_id))
        else:
            branch, wt, cwd, archive_dir = await asyncio.to_thread(
                dev_pipeline.create_worktree, req_id)
        job_id = await asyncio.to_thread(
            self._add_job, req_id, branch, wt)
        self._active_job_ids[req_id] = job_id   # 例外時にfailed化するための控え
        # 2) claude 実装（別スレッド）＋進捗ティッカー
        progress.set_phase("実装中")

        def on_event(ev):
            label = dev_pipeline.classify_event(ev)
            if label:
                progress.add_op(label)

        guidelines, lessons = await asyncio.to_thread(self._prompt_inputs)
        prompt = dev_pipeline.build_prompt(cap_req, guidelines, lessons,
                                           resume=resume)
        ticker = self._spawn(self._tick_progress(prog, progress))
        try:
            run = await asyncio.to_thread(
                dev_pipeline.stream_claude, prompt, cwd, on_event)
        finally:
            ticker.cancel()
        # 3) 検証
        progress.set_phase("テスト＋pyflakes実行中")
        await self._safe_edit(prog, persona.job_phase_test())
        await asyncio.to_thread(dev_pipeline.stage_all, wt)   # 新規ファイルもdiff対象に
        files = await asyncio.to_thread(dev_pipeline.changed_files, wt)
        test_ok, test_tail = await asyncio.to_thread(
            dev_pipeline.run_tests, wt, files)
        flakes_ok, flakes_tail = await asyncio.to_thread(
            dev_pipeline.run_pyflakes, wt, files)
        dstat = await asyncio.to_thread(dev_pipeline.diff_stat, wt)
        if not files and not run.get("error"):
            # 差分ゼロの「成功」を承認待ちに乗せない（👍で起票だけ閉じる事故防止）
            run = dict(run, error="変更が生成されませんでした（差分なし）")
        # 4) サマリー投稿（👍承認待ち）。中断エラーでも差分あり＋検証緑なら
        #    salvage＝builtとして承認待ちに乗せる（完成品をfailedで捨てない）
        salvaged = bool(run.get("error")) and bool(files) and test_ok and flakes_ok
        summary = dev_pipeline.summarize(
            cap_req, test_ok=test_ok, test_tail=test_tail, flakes_ok=flakes_ok,
            flakes_tail=flakes_tail, diff_stat=dstat,
            final_text=run.get("final_text", ""), error=run.get("error"),
            salvaged=salvaged, warnings=dev_pipeline.risk_warnings(files))
        summary_msg = await self._post_summary(summary, wt, req_id)
        built = (not run.get("error")) or salvaged
        if built and summary_msg is None:
            # サマリーが投稿できないと👍の手段が無く、builtのまま孤児化する。
            # failedにしておけば「続きから」で安価にやり直せる
            built = False
            run = dict(run, error="サマリー投稿に失敗（承認手段が無いため失敗扱い）")
        status = "built" if built else "failed"
        await asyncio.to_thread(
            self._finish_job, job_id, status,
            summary_msg.id if summary_msg else None, summary)
        if status == "failed":
            # 失敗理由は教訓として自動記録（次のジョブのプロンプトに注入される）。
            # モデル出力由来の自由文はそのまま永続させない（注入の持ち込み対策）
            err = run.get("error") or "不明"
            if not any(k in err for k in ("中断しました", "exit=", "差分なし",
                                          "サマリー投稿に失敗")):
                err = "claude実行エラー（詳細はジョブサマリー参照）"
            await asyncio.to_thread(
                self._add_lesson, req_id, job_id, "failed",
                f"起票#{req_id}「{cap_req['description'][:60]}」: {err[:200]}")
        self._active_job_ids.pop(req_id, None)   # 正常終了＝例外時failed化は不要
        await self._safe_edit(prog, persona.job_finished(req_id, built))

    async def _tick_progress(self, prog, progress):
        """4秒ごとに進捗メッセージを更新（1本をedit＝spam回避）。"""
        try:
            while True:
                await asyncio.sleep(4)
                await self._safe_edit(prog, persona.progress(*progress.snapshot()))
        except asyncio.CancelledError:
            pass

    async def _post_summary(self, summary, wt, req_id):
        channel = self._channel()
        if channel is None:
            return None
        raw = await asyncio.to_thread(dev_pipeline.full_diff, wt)
        files = []
        if raw.strip():
            data = io.BytesIO(raw.encode("utf-8"))
            files = [discord.File(data, filename=f"cap{req_id}.diff")]
        try:
            return await channel.send(summary[:1900], files=files)
        except discord.DiscordException as e:
            print(f"サマリー投稿失敗: {e}")
            return None

    def _add_job(self, req_id, branch, wt):
        with db.connect(DB_PATH) as conn:
            return db.add_dev_job(conn, cap_req_id=req_id, branch=branch,
                                  worktree=wt, channel_id=self.dev_channel_id,
                                  created_at=_now_iso())

    @staticmethod
    def _finish_job(job_id, status, message_id, summary):
        with db.connect(DB_PATH) as conn:
            db.update_dev_job(conn, job_id, updated_at=_now_iso(),
                              status=status, message_id=message_id,
                              summary=(summary or "")[:1000])

    # --- 進化ロードマップ（カード提案→👍👎トリアージ） ----------------------
    async def _maybe_post_card(self):
        """未提案の最上位カードを1枚投稿する（提案中があれば何もしない）。
        投稿できたら True。"""
        item = await asyncio.to_thread(roadmap.pick_next, DB_PATH)
        if item is None:
            return False
        channel = self._channel()
        if channel is None:
            return False
        try:
            msg = await channel.send(roadmap.format_card(item))
        except discord.DiscordException as e:
            print(f"カード投稿失敗: {e}")
            return False
        marked = await asyncio.to_thread(
            roadmap.mark_proposed, DB_PATH, item["id"], msg.id)
        if not marked:      # 競合（同時投稿）に負けたら重複カードを消す
            try:
                await msg.delete()
            except discord.DiscordException:
                pass
            return False
        return True

    async def _handle_roadmap_reaction(self, payload, emoji):
        """カードへの👍👎を処理。カード対象だったら True（ジョブ承認と区別）。
        !roadmap要約への👍👎も「いま提案中のカード」への判断として扱う。"""
        d = await asyncio.to_thread(
            roadmap.decide, DB_PATH, payload.message_id,
            emoji == "👍", payload.user_id)
        if d is None and payload.message_id in self._summary_msg_ids:
            d = await asyncio.to_thread(
                roadmap.decide_current, DB_PATH, emoji == "👍",
                payload.user_id)
        if d is None:
            return False
        item, cap_id = d["item"], d["cap_req_id"]
        if d["action"] == "skipped":
            await self._notify(persona.roadmap_skipped(item["id"]))
        elif d["action"] == "queued_session":
            await self._notify(persona.roadmap_session(item["id"]))
        elif self._running_jobs:
            # 実装リソースは1本ずつ（並列claudeを避ける）。起票済みなので手動でも呼べる
            await self._notify(persona.roadmap_queued(item["id"], cap_id))
        else:
            await self._notify(persona.roadmap_started(item["id"], cap_id))
            self._spawn(self._run_dev_job(cap_id))
        if not await self._maybe_post_card():
            counts = await asyncio.to_thread(self._roadmap_pending_count)
            if counts == 0:
                await self._notify(persona.roadmap_all_done())
        return True

    @staticmethod
    def _roadmap_pending_count():
        with db.connect(DB_PATH) as conn:
            return db.roadmap_counts(conn).get("pending", 0)

    # --- 週次開発レポート（RM#60）: 開発実績を相談室へ自己開示 ---------------
    async def _maybe_dev_report(self):
        """金曜18時台に1回、この1週間の開発実績を相談室へ報告する（決定論）。"""
        cfg = (self.dev_cfg.get("weekly_report") or {})
        if not cfg.get("enabled"):
            return
        if not await asyncio.to_thread(
                dev_report.should_send, DB_PATH,
                weekday=int(cfg.get("weekday", dev_report.WEEKDAY_DEFAULT)),
                hour=int(cfg.get("hour", dev_report.HOUR_DEFAULT))):
            return
        data = await asyncio.to_thread(dev_report.collect, DB_PATH)
        channel_id = int(cfg.get("channel_id", self.dev_channel_id))
        channel = self.get_channel(channel_id)
        if channel is None:
            print(f"dev report channel {channel_id} not found")
            return
        await channel.send(dev_report.build_report(data))
        await asyncio.to_thread(dev_report.mark_sent, DB_PATH)
        print("[dev report] weekly report sent")

    # --- 起票の自動拾い上げ（RM#21）: 自己進化ループを2クリックで閉じる ------
    async def _maybe_propose_cap(self):
        """エージェントのオーガニック起票（open）を検出して着手提案を投稿する。"""
        cap = await asyncio.to_thread(roadmap.watch_next_cap, DB_PATH)
        if cap is None:
            return
        channel = self._channel()
        if channel is None:
            return
        msg = await channel.send(roadmap.format_cap_proposal(cap))
        await asyncio.to_thread(
            roadmap.mark_cap_proposed, DB_PATH, cap["id"], msg.id)
        print(f"cap proposal posted: 起票#{cap['id']}")

    async def _handle_cap_proposal_reaction(self, payload, emoji):
        """起票提案への👍👎を処理。対象だったら True。"""
        d = await asyncio.to_thread(
            roadmap.decide_cap, DB_PATH, payload.message_id, emoji == "👍")
        if d is None:
            return False
        cap_id = d["cap_id"]
        if not d["approved"]:
            await self._notify(persona.cap_declined(cap_id))
        elif self._running_jobs:
            await self._notify(persona.cap_queued(cap_id))
        else:
            await self._notify(persona.cap_accepted(cap_id))
            self._spawn(self._run_dev_job(cap_id))
        return True

    # --- Phase 3: 承認ゲート（👍反映→再起動→復帰報告 / 👎却下 / 失敗ロールバック） ---
    async def on_raw_reaction_add(self, payload):
        if (payload.channel_id != self.dev_channel_id
                or str(payload.user_id) not in self.admins):
            return
        emoji = str(payload.emoji)
        if emoji not in ("👍", "👎"):
            return
        # ロードマップカード・起票提案への反応を先に判定（ジョブ承認とは別扱い）
        try:
            if await self._handle_roadmap_reaction(payload, emoji):
                return
        except Exception as e:
            print(f"roadmap reaction error: {e}")
        try:
            if await self._handle_cap_proposal_reaction(payload, emoji):
                return
        except Exception as e:
            print(f"cap proposal reaction error: {e}")
        job = await asyncio.to_thread(self._job_by_msg, payload.message_id)
        if job is None or job["status"] != "built":
            return    # 承認待ち(built)のサマリーへのリアクションだけ扱う
        # compare-and-set で排他: 二重👍・👍と👎の同時押しは片方だけが勝つ
        to_status = "rejected" if emoji == "👎" else "deploying"
        claimed = await asyncio.to_thread(
            self._claim_job, job["id"], "built", to_status)
        if not claimed:
            return
        if emoji == "👎":
            self._spawn(self._reject_job(job))
        else:
            self._spawn(self._approve_job(job))

    @staticmethod
    def _job_by_msg(message_id):
        with db.connect(DB_PATH) as conn:
            return db.get_dev_job_by_message(conn, message_id)

    @staticmethod
    def _set_job_status(job_id, status, *, cap_req_id=None, close_cap=False):
        with db.connect(DB_PATH) as conn:
            db.update_dev_job(conn, job_id, updated_at=_now_iso(), status=status)
            if close_cap and cap_req_id is not None:
                db.set_capability_status(conn, cap_req_id, "deployed")

    async def _reject_job(self, job):
        await self._notify(persona.rejected(job["cap_req_id"]))

        def _do():
            deploy.cleanup(job["worktree"], job["branch"])
            self._set_job_status(job["id"], "rejected")
        await asyncio.to_thread(_do)
        # 却下理由を教訓として集める（返信は任意。集まれば次のジョブに効く）
        channel = self._channel()
        if channel is not None:
            try:
                ask = await channel.send(
                    persona.ask_reject_reason(job["cap_req_id"]))
                self._lesson_prompts[ask.id] = (job["cap_req_id"], job["id"])
                while len(self._lesson_prompts) > 20:   # 放置分は古い順に破棄
                    self._lesson_prompts.pop(next(iter(self._lesson_prompts)))
            except discord.DiscordException as e:
                print(f"理由聞き送信失敗: {e}")

    async def _approve_job(self, job):
        channel = self._channel()
        if channel is None:
            return
        req_id = job["cap_req_id"]
        msg = await channel.send(persona.approving(req_id))
        pre_sha = await asyncio.to_thread(deploy.current_head)
        # merge完了後のHEAD（例外時に「mergeまで進んだか」「その後に人間のコミットが
        # 入っていないか」を判定してからロールバックするための控え）
        state = {"post_sha": None}
        try:
            await self._deploy_flow(job, req_id, msg, pre_sha, state)
        except Exception as e:
            print(f"deploy error: {e}")
            await asyncio.to_thread(self._set_job_status, job["id"], "failed")
            if state["post_sha"] is None:
                # merge前に落ちた＝mainは無傷
                await self._safe_edit(msg, persona.deploy_failed(
                    req_id, f"予期しないエラー: {e}（mainは変更前のままっす）"))
                return
            ok, out = await asyncio.to_thread(
                deploy.rollback, pre_sha, state["post_sha"])
            note = ("ロールバックしました" if ok
                    else f"自動ロールバック失敗＝要手動確認（{out}）")
            await self._safe_edit(msg, persona.deploy_failed(
                req_id, f"予期しないエラー: {e}。{note}"))

    async def _deploy_flow(self, job, req_id, msg, pre_sha, state):
        cap = await asyncio.to_thread(self._load_cap_req, req_id)
        desc = (cap or {}).get("description", "")
        # 1) worktreeをコミット（失敗のまま進むと空mergeを「反映済み」と誤報告する）
        committed = await asyncio.to_thread(
            deploy.commit_worktree, job["worktree"], req_id, desc)
        if not committed:
            await asyncio.to_thread(self._set_job_status, job["id"], "failed")
            await self._safe_edit(msg, persona.deploy_failed(
                req_id, "worktreeのコミットに失敗（空反映を防ぐため中断）"))
            return
        # mainの未コミット手修正と重なるとgitがmergeを拒否する（起票#7で実証）。
        # 先に検出して明快に伝え、builtへ戻す＝解消後にもう一回👍で反映できる
        blockers = await asyncio.to_thread(self._merge_blockers, job["branch"])
        if blockers:
            names = [os.path.basename(f) for f in blockers]
            await asyncio.to_thread(self._set_job_status, job["id"], "built")
            await self._safe_edit(msg,
                                  persona.deploy_blocked_dirty(req_id, names))
            return
        merged, mout = await asyncio.to_thread(deploy.merge_branch, job["branch"])
        if not merged:
            print(f"merge失敗: {mout}")
            await asyncio.to_thread(self._set_job_status, job["id"], "failed")
            await self._safe_edit(
                msg, persona.deploy_failed(req_id, "merge競合のため中断（手動確認を）"))
            return
        state["post_sha"] = await asyncio.to_thread(deploy.current_head)
        # 2) live で回帰テスト（変更に応じたスイート。赤ならロールバック）
        files = await asyncio.to_thread(deploy.merged_files, pre_sha)
        tests_ok, ttail = await asyncio.to_thread(deploy.run_live_tests, files)
        if not tests_ok:
            print(f"liveテスト赤: {ttail}")
            await self._rollback_and_report(job, req_id, msg, pre_sha, state,
                                            why="liveテスト赤", kick=[])
            return
        # 3) 変更が載っているプロセスだけ再起動→復帰確認（失敗なら自動ロールバック）
        targets = dev_pipeline.restart_targets(files)
        if "archivebot" in targets:
            await self._safe_edit(msg, persona.restarting("archivebot"))
            await asyncio.to_thread(self._kickstart, "com.discord.archivebot")
            if not await self._await_archivebot_recovery():
                await self._rollback_and_report(
                    job, req_id, msg, pre_sha, state,
                    why="archivebotが復帰せず", kick=["com.discord.archivebot"])
                return
        if "meetingbot" in targets:
            # meetingbotはアカウント共有でpresence確認不可→プロセス生存で確認
            await self._safe_edit(msg, persona.restarting("meetingbot"))
            await asyncio.to_thread(self._kickstart, "com.discord.meetingbot")
            if not await self._await_process_alive("com.discord.meetingbot"):
                kick = ["com.discord.meetingbot"]
                if "archivebot" in targets:
                    kick.append("com.discord.archivebot")  # 新コードのまま残さない
                await self._rollback_and_report(
                    job, req_id, msg, pre_sha, state,
                    why="meetingbotが復帰せず", kick=kick)
                return
        # 4) 成功: worktree掃除＋deployed＋起票クローズ＋デプロイ記録（revert/カナリア用）
        await asyncio.to_thread(deploy.cleanup, job["worktree"], job["branch"])
        await asyncio.to_thread(self._set_job_status, job["id"], "deployed",
                                cap_req_id=req_id, close_cap=True)
        await asyncio.to_thread(
            self._record_deploy, job["id"], req_id, pre_sha,
            state["post_sha"], files)
        await self._safe_edit(msg, persona.deployed(req_id))
        if "devbot" in targets:
            # 自分自身の変更はexitで反映（launchd KeepAliveが再起動してくれる）。
            # 実行中ジョブを道連れにしないよう、空くのを待ってから落ちる
            self._spawn(self._self_restart())

    @staticmethod
    def _record_deploy(job_id, cap_req_id, pre_sha, post_sha, files):
        """デプロイ履歴を記録（!revert とカナリア監視の台帳）。"""
        with db.connect(DB_PATH) as conn:
            db.add_deploy_record(
                conn, job_id=job_id, cap_req_id=cap_req_id, pre_sha=pre_sha,
                post_sha=post_sha, files="\n".join(files or []),
                deployed_at=_now_iso(),
                canary_baseline=deploy.err_log_size())

    # --- !revert: デプロイ後の巻き戻し（管理者コマンド） --------------------
    async def _revert_flow(self, cap_id):
        rec = await asyncio.to_thread(self._deploy_record_for, cap_id)
        if rec is None:
            await self._notify(persona.revert_not_found(cap_id))
            return
        msg = await self._channel().send(persona.reverting(cap_id))
        ok, out = await asyncio.to_thread(
            deploy.revert_deploy, rec["pre_sha"], rec["post_sha"])
        if not ok:
            await self._safe_edit(msg, persona.revert_failed(cap_id, out[:200]))
            return
        files = [f for f in (rec["files"] or "").split("\n") if f]
        tests_ok, _tail = await asyncio.to_thread(deploy.run_live_tests, files)
        for name, label in (("archivebot", "com.discord.archivebot"),
                            ("meetingbot", "com.discord.meetingbot")):
            if name in dev_pipeline.restart_targets(files):
                await asyncio.to_thread(self._kickstart, label)
        await asyncio.to_thread(self._mark_reverted, rec["job_id"])
        await self._safe_edit(msg, persona.reverted(cap_id, tests_ok))
        if "devbot" in dev_pipeline.restart_targets(files):
            self._spawn(self._self_restart())

    @staticmethod
    def _deploy_record_for(cap_id):
        with db.connect(DB_PATH) as conn:
            return db.latest_deploy_for_cap(conn, cap_id)

    @staticmethod
    def _mark_reverted(job_id):
        with db.connect(DB_PATH) as conn:
            db.mark_deploy_reverted(conn, job_id, _now_iso())
            db.update_dev_job(conn, job_id, updated_at=_now_iso(),
                              status="reverted")

    # --- デプロイ後カナリア監視（約15分ごと・監視ループから呼ばれる） --------
    async def _canary_check(self):
        def _scan():
            alerts = []
            now = datetime.datetime.now()
            with db.connect(DB_PATH) as conn:
                for rec in db.watching_canaries(conn):
                    try:
                        deployed = datetime.datetime.fromisoformat(
                            rec["deployed_at"])
                    except (TypeError, ValueError):
                        db.set_canary_status(conn, rec["job_id"], "ok")
                        continue
                    verdict = monitor.canary_verdict(
                        rec["canary_baseline"] or 0, deploy.err_log_size(),
                        (now - deployed).total_seconds())
                    if verdict == "ok":
                        db.set_canary_status(conn, rec["job_id"], "ok")
                    elif verdict == "alert":
                        db.set_canary_status(conn, rec["job_id"], "alerted")
                        grown = deploy.err_log_size() \
                            - (rec["canary_baseline"] or 0)
                        alerts.append((rec["cap_req_id"], grown // 1024))
            return alerts
        for cap_id, grown_kb in await asyncio.to_thread(_scan):
            await self._notify(persona.canary_alert(cap_id, grown_kb))

    async def _rollback_and_report(self, job, req_id, msg, pre_sha, state, *,
                                   why, kick):
        """rollback→（必要な）再kickstart→failed化→結果を正直に報告。"""
        ok, out = await asyncio.to_thread(deploy.rollback, pre_sha,
                                          state.get("post_sha"))
        for label in kick:
            await asyncio.to_thread(self._kickstart, label)
        await asyncio.to_thread(self._set_job_status, job["id"], "failed")
        if ok:
            await self._safe_edit(msg, persona.deploy_rolled_back(req_id, why))
        else:
            await self._safe_edit(msg, persona.deploy_failed(
                req_id, f"{why}。さらに自動ロールバック失敗＝要手動確認（{out}）"))

    @staticmethod
    def _merge_blockers(branch):
        return deploy.merge_blockers(deploy.dirty_files(),
                                     deploy.branch_files(branch))

    @staticmethod
    def _kickstart(label):
        subprocess.run(
            ["launchctl", "kickstart", "-k",
             f"gui/{os.getuid()}/{label}"], capture_output=True)

    async def _await_process_alive(self, label, *, timeout_sec=45, step=3,
                                   settle_sec=12):
        """kickstart後の生存確認（presenceが使えない対象用）。KeepAliveの
        crash-loopを「復帰」と誤認しないよう、PIDが現れた後 settle_sec 待って
        同じPIDのまま生きていることまで確認する。"""
        for _ in range(max(1, timeout_sec // step)):
            await asyncio.sleep(step)
            pid, _ex = await asyncio.to_thread(monitor.launchctl_probe, label)
            if pid is not None:
                await asyncio.sleep(settle_sec)
                pid2, _ex2 = await asyncio.to_thread(
                    monitor.launchctl_probe, label)
                return pid2 == pid
        return False

    async def _self_restart(self):
        """devbot自身の変更を反映するための自己再起動（Phase 3の自己進化の輪）。"""
        while self._running_jobs:
            await asyncio.sleep(10)
        await self._notify(persona.self_restarting())
        try:
            await self.close()
        finally:
            sys.stdout.flush()   # launchd経由のstdoutはブロックバッファリング
            sys.stderr.flush()
            os._exit(0)   # launchd(KeepAlive)が新コードで再起動する

    async def _await_archivebot_recovery(self, *, timeout_sec=90, step=3):
        """再起動後、archivebotがDiscordにオンライン復帰するまで待つ（presence主信号）。"""
        target = next((t for t in self.targets if t["name"] == "archivebot"),
                      None)
        if target is None:
            return True
        names = target.get("presence_bot_names", [])
        label = target["launchd_label"]
        for _ in range(max(1, timeout_sec // step)):
            await asyncio.sleep(step)
            pid, _ex = await asyncio.to_thread(monitor.launchctl_probe, label)
            if pid is not None and self._presence_online(names):
                return True
        return False


def main():
    discord.utils.setup_logging(level=logging.WARNING)
    guild_id, admins, dev_cfg = load_dev_config()
    token = dev_cfg.get("token")
    if not token or token == TOKEN_PLACEHOLDER:
        sys.exit("dev_bot.token が未設定です（config.json に実トークンを貼ってください）")

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.guilds = True
    intents.presences = True          # ← 監視の主信号（Botのオンライン状態）に必須
    intents.reactions = True          # ← Phase 3 承認ゲート（👍/👎）

    client = DevBot(guild_id=guild_id, admins=admins, dev_cfg=dev_cfg,
                    intents=intents)
    client.run(token, log_level=logging.WARNING)


if __name__ == "__main__":
    main()
