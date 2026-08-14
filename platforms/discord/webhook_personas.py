#!/usr/bin/env python3
"""
Webhook試用枠人格（エージェントv2 Phase 2）の応答と人事ライフサイクル。

archiver（アーカイブ担当）に mix-in される。責務は「試用枠人格（Webhook人格）」に閉じる:
  - ホームchでの人間発言に、その人格として（runner経路で）応答する
  - AI人事の [HIRE:]/[FIRE:] を提案に変換し、管理者👍でspawn/退役を実行する

判断（誰を採る/切る）は人間が握る＝提案と実行を分離する（設計思想メモ）。
本物Botの発行やコード改変はしない（手足のみ、脳と安全弁は触らせない）。

self._apply_rule_markers / self._update_thread_summary など一部は
AgentClient本体（bot.py）側のメソッドを MRO 経由で使う。
"""

import asyncio
import os
import re

import discord

from core import db
from core import hr
from core import plugins
from core import reminders
from core import rules
from core import runner_answer
from core import search
from platforms.discord import webhooks
from platforms.discord.agent_runtime import (
    ADMIN_IDS,
    AGENT_CATEGORY_ID,
    AGENTS,
    ALLOWED_MENTIONS,
    ANSWER_SEM,
    APPROVE_REACTIONS,
    DB_PATH,
    GUILD_ID,
    IMAGE_SEM,
    PLUGINS,
    WEBHOOK_AGENTS,
    WEBHOOK_HOME_CHANNELS,
    _best_effort_typing,
    _current_roster,
    _has_skill,
    _load_webhook_agents,
    _resolve,
    _webhook_cache,
    fetch_history,
    registered_bot_ids,
    should_webhook_respond,
)


class WebhookPersonaMixin:
    """Webhook試用枠人格の応答＋採用/解雇。AgentClient(archiver)に混ぜて使う。"""

    async def _maybe_webhook_persona(self, message):
        """Webhook人格のホームチャンネルの人間発言に、その人格として応答する。
        既存3体の挙動には影響しない（WEBHOOK_AGENTSが空ならno-op）。"""
        agent_id = WEBHOOK_HOME_CHANNELS.get(message.channel.id)
        if not should_webhook_respond(
                has_webhook_agents=bool(WEBHOOK_AGENTS),
                home_agent_id=agent_id,
                webhook_id=message.webhook_id,
                author_is_bot=message.author.bot,
                author_id=message.author.id,
                self_user_id=self.user.id if self.user else None,
                mention_ids=[u.id for u in message.mentions],
                registered_ids=registered_bot_ids(),
                text=message.clean_content):
            return
        await self._respond_as_webhook(message, WEBHOOK_AGENTS[agent_id])

    def _build_webhook_agent_param(self, wa, message):
        """Webhook人格用の agent dict（ペルソナ＋有効ルール＋保存/誠実失敗ノート）。
        Web検索は runner 経路で常時有効。返り値は agent dict。"""
        persona_path = _resolve(wa["persona_file"])
        scopes = rules.context_scopes(message.channel.id, message.author.id)
        now = reminders.fmt(reminders.now_jst())
        with db.connect(DB_PATH) as conn:
            active = db.get_active_rules(conn, wa["id"], scopes, now=now)
        # 発言者は人間限定（_maybe_webhook_personaのゲートで保証済み）
        parts = []
        if active:
            parts.append(rules.build_rules_block(active))
        parts.append(rules.build_skill_note(active))
        plugin_note = plugins.build_skill_notes(PLUGINS)
        if plugin_note:
            parts.append(plugin_note)
        # 採用スキル（AI人事）: 現メンバー＋解雇可能な試用枠を添えて指示を注入
        if _has_skill(wa, "hire"):
            fireable = [(a["id"], a["name"]) for a in WEBHOOK_AGENTS.values()
                        if a["id"] != wa["id"]]
            parts.append(hr.build_skill_note(_current_roster(), fireable))
        return {"name": wa["name"], "persona_files": [persona_path],
                "role": "\n".join(p for p in parts if p)}

    async def _respond_as_webhook(self, message, wa):
        """runner経路で回答を作り、Webhookで人格名・アイコンを差し替えて投稿。"""
        try:
            history = await fetch_history(message)
            agent_param = self._build_webhook_agent_param(wa, message)
            async with ANSWER_SEM:
                async with _best_effort_typing(message.channel):  # 折衷: typingはアーカイブ担当名義
                    result = await asyncio.to_thread(
                        runner_answer.answer_question, DB_PATH, str(GUILD_ID),
                        message.clean_content.strip(), search.DEFAULT_MODEL,
                        message.channel.id, history, agent_param, None)
            answer = result["answer"]
            answer = self._apply_rule_markers(
                message, answer, agent_id=wa["id"])
            # 採用スキル持ち（AI人事）は [HIRE:]/[FIRE:] を提案に変換
            hires, fires = [], []
            if _has_skill(wa, "hire"):
                answer, hires = self._extract_hires(message, answer)
                answer, fires = self._extract_fires(message, wa, answer)
            # プラグインスキルのマーカーを実行（人間発言のみ・別スレッド）
            if PLUGINS and not message.author.bot:
                answer, plugin_out = await asyncio.to_thread(
                    plugins.apply_markers, PLUGINS, answer)
                if plugin_out:
                    answer = (answer + "\n" if answer else "") \
                        + "\n".join(plugin_out)
            if answer:
                await webhooks.post_as_persona(
                    message.channel, name=wa["name"], content=answer,
                    cache=_webhook_cache,
                    avatar_url=wa.get("avatar_url"),
                    avatar_bytes=wa.get("avatar_bytes"),
                    allowed_mentions=ALLOWED_MENTIONS)
            for h in hires:
                await self._post_hire_proposal(message, wa, h)
            for tgt in fires:
                await self._post_fire_proposal(message, wa, tgt)
            task = asyncio.create_task(
                self._update_thread_summary(message.channel.id))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception as e:
            print(f"[webhook:{wa['id']}] answer failed: {e}")
            try:
                await webhooks.post_as_persona(
                    message.channel, name=wa["name"],
                    avatar_url=wa["avatar_url"],
                    content=f"⚠️ ごめんなさい、うまく答えられませんでした"
                            f"（{str(e)[:150]}）",
                    cache=_webhook_cache)
            except Exception:
                pass

    def _extract_hires(self, message, answer):
        """[HIRE:] マーカーを抽出・除去し、検証を通った採用案リストを返す。
        検証NGは本文に -# 行で理由を残す。返り値 (本文, 採用案[])。"""
        text, hires, errors = hr.extract_markers(answer)
        reals = AGENTS
        real_ids = {a["id"] for a in reals}
        real_names = {a["name"] for a in reals}
        with db.connect(DB_PATH) as conn:
            existing = db.get_active_agents(conn)
        valid, notes = [], []
        for h in hires:
            err = hr.validate_hire(h, real_ids=real_ids, real_names=real_names,
                                   existing_agents=existing)
            if err:
                notes.append(f"-# ⚠️ 採用できません: {err}")
            else:
                valid.append(h)
        for e in errors:
            notes.append(f"-# ⚠️ 採用提案の形式エラー: {e}")
        if notes:
            text = (text + "\n" if text else "") + "\n".join(notes)
        return text, valid

    async def _post_hire_proposal(self, message, wa, hire):
        """採用提案を投稿し、pending_hires に承認待ちとして保存する。
        既存チャンネルがあれば紐付け、無ければ承認時に作成する旨を明示する。
        チャンネル名は実行時と同じ ai 接頭辞つきで解決する（提案と実行のズレ防止）。"""
        ch_name = hr.ai_channel_name(hire["channel_name"])
        existing_ch = discord.utils.get(message.guild.text_channels,
                                        name=ch_name)
        ch_id = existing_ch.id if existing_ch else None
        where = (f"<#{ch_id}>" if ch_id
                 else f"#{ch_name}（無いので新規作成します）")
        text = (f"📋 採用のご提案です。\n"
                f"**{hire['name']}** — {hire['role']}\n"
                f"配属先: {where}\n"
                f"-# 管理者が 👍 で承認したら採用します（却下ならそのまま）")
        msg = await webhooks.post_as_persona(
            message.channel, name=wa["name"], content=text,
            cache=_webhook_cache, avatar_bytes=wa.get("avatar_bytes"),
            avatar_url=wa.get("avatar_url"), wait=True)
        if msg is None:
            return
        with db.connect(DB_PATH) as conn:
            db.add_pending_hire(
                conn, message_id=msg.id, new_id=hire["new_id"],
                name=hire["name"], role=hire["role"],
                channel_name=hire["channel_name"], channel_id=ch_id,
                proposed_by=str(message.author.id),
                created_at=reminders.fmt(reminders.now_jst()))
        print(f"[hire] proposal posted: {hire['new_id']} (msg={msg.id})")

    def _hr_persona_name(self):
        """hireスキルを持つWebhook人格の表示名（完了報告の名義）。既定 AI人事。"""
        for wa in WEBHOOK_AGENTS.values():
            if _has_skill(wa, "hire"):
                return wa["name"]
        return "AI人事"

    def _extract_fires(self, message, wa, answer):
        """[FIRE:] マーカーを抽出・除去し、検証を通った解雇対象idを返す。
        検証NGは本文に -# 行で理由を残す。返り値 (本文, 対象id[])。"""
        text, ids = hr.extract_fires(answer)
        if not ids:
            return text, []               # マーカー無しはDB接続しない
        with db.connect(DB_PATH) as conn:
            existing = db.get_active_agents(conn)
        valid, notes = [], []
        for tid in ids:
            err, _ = hr.validate_fire(tid, existing_agents=existing,
                                      hr_agent_id=wa["id"])
            if err:
                notes.append(f"-# ⚠️ 解雇できません: {err}")
            else:
                valid.append(tid)
        if notes:
            text = (text + "\n" if text else "") + "\n".join(notes)
        return text, valid

    async def _post_fire_proposal(self, message, wa, target_id):
        """解雇提案を投稿し、pending_fires に承認待ちとして保存する。"""
        with db.connect(DB_PATH) as conn:
            target = db.get_agent(conn, target_id)
        if target is None or target["status"] != "active":
            return
        # 採用時に専用に作った部屋なら、退役と一緒に削除する
        ch_note = ("配属チャンネルも一緒に削除します"
                   if target["home_channel_created"]
                   else "配属チャンネルは残ります")
        text = (f"🗂 解雇のご提案です。\n"
                f"**{target['name']}**（{target_id}）を退役させます。\n"
                f"-# 管理者が 👍 で承認したら退役します（{ch_note}）")
        msg = await webhooks.post_as_persona(
            message.channel, name=wa["name"], content=text,
            cache=_webhook_cache, avatar_bytes=wa.get("avatar_bytes"),
            avatar_url=wa.get("avatar_url"), wait=True)
        if msg is None:
            return
        with db.connect(DB_PATH) as conn:
            db.add_pending_fire(
                conn, message_id=msg.id, target_id=target_id,
                target_name=target["name"],
                proposed_by=str(message.author.id),
                created_at=reminders.fmt(reminders.now_jst()))
        print(f"[fire] proposal posted: {target_id} (msg={msg.id})")

    async def _maybe_approve_fire(self, payload):
        """解雇提案への管理者👍を承認として処理し、退役を実行する。"""
        if payload.guild_id != GUILD_ID:
            return
        if str(payload.user_id) not in ADMIN_IDS:
            return
        if str(payload.emoji) not in APPROVE_REACTIONS:
            return
        with db.connect(DB_PATH) as conn:
            fire = db.get_pending_fire_by_message(conn, payload.message_id)
            if fire is None:
                return
            if not db.claim_pending_fire(conn, fire["id"]):
                return  # 既に別リアクションが確保済み
        await self._execute_fire(payload, fire)

    async def _execute_fire(self, payload, fire):
        """承認された解雇を実行: 退役→（採用時に作った専用部屋なら削除）→
        再起動なし反映→完了報告。アーカイブDBの過去ログは削除しない。"""
        proposal_ch = self.get_channel(payload.channel_id)
        hr_name = self._hr_persona_name()
        # 退役前に対象の部屋情報を取得（削除判断用）
        with db.connect(DB_PATH) as conn:
            target = db.get_agent(conn, fire["target_id"])
            retired = db.retire_agent(conn, fire["target_id"])
            db.set_fire_status(conn, fire["id"], "done" if retired else "error")
        _load_webhook_agents()  # 退役を即反映（応答停止）
        print(f"[fire] retired {fire['target_id']} (ok={retired})")

        # 採用時に作った専用チャンネルは削除（他の人格が居れば残す）。
        # archive.db の過去ログは消さない（ナレッジとして検索可能なまま）。
        deleted = False
        if retired and target and target["home_channel_created"]:
            ch_id = int(target["home_channel_id"])
            others = ch_id in WEBHOOK_HOME_CHANNELS  # reload後: 他人格が居るか
            if not others:
                try:
                    ch = self.get_guild(GUILD_ID).get_channel(ch_id)
                    if ch is not None:
                        await ch.delete(reason=f"退役: {fire['target_id']}")
                        deleted = True
                except Exception as e:
                    print(f"[fire] channel delete failed: {e}")
        try:
            if retired:
                note = ("配属チャンネルも削除しました" if deleted
                        else "部屋は残してあります")
                await webhooks.post_as_persona(
                    proposal_ch, name=hr_name, cache=_webhook_cache,
                    content=f"✅ **{fire['target_name']}** を退役させました。"
                            f"お疲れさまでした（{note}）。")
            else:
                await webhooks.post_as_persona(
                    proposal_ch, name=hr_name, cache=_webhook_cache,
                    content=f"⚠️ {fire['target_name']} は既に退役済みでした。")
        except Exception as e:
            print(f"[fire] notice failed: {e}")

    async def _maybe_approve_hire(self, payload):
        """採用提案への管理者👍を承認として処理し、spawnを実行する。"""
        if payload.guild_id != GUILD_ID:
            return
        if str(payload.user_id) not in ADMIN_IDS:
            return  # 承認は管理者（管理者）のみ
        if str(payload.emoji) not in APPROVE_REACTIONS:
            return  # 承認は👍のみ（何気ない❤️等で確定させない）
        # 承認をアトミックに確保（二重承認→二重spawnを防ぐ）
        with db.connect(DB_PATH) as conn:
            hire = db.get_pending_hire_by_message(conn, payload.message_id)
            if hire is None:
                return
            if not db.claim_pending_hire(conn, hire["id"]):
                return  # 既に別リアクションが確保済み
        await self._execute_hire(payload, hire)

    def _generate_agent_avatar(self, new_id, name, role):
        """新エージェントのアイコンをCodexで生成し avatars/<id>.png に保存する
        （同期・to_thread前提）。成功で相対パス、失敗で None。デフォルト
        アイコンは使わない方針なので、生成できた場合のみ設定する。"""
        import shutil
        import subprocess
        imagegen = agent_runtime.load_imagegen(None)
        if imagegen is None:
            return None   # 画像生成の連携が無ければアイコンは付けない
        prompt = (
            f"アニメ調のかわいいキャラクターのプロフィールアイコン。"
            f"{role}を担当するスタッフの女の子、親しみやすい笑顔、"
            "正方形のアイコン向け、クリーンで爽やかな配色、上半身のバストアップ、"
            "背景はシンプル")
        try:
            src = imagegen.generate(prompt)   # 生成画像のパス
            dest_rel = os.path.join("avatars", f"{new_id}.png")
            dest = _resolve(dest_rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            r = subprocess.run(["sips", "-Z", "256", src, "--out", dest],
                               capture_output=True)
            if r.returncode != 0 or not os.path.exists(dest):
                shutil.copyfile(src, dest)            # 縮小失敗時はそのままコピー
            return dest_rel
        except Exception as e:
            print(f"[hire] avatar generation failed: {e}")
            return None

    def _revalidate_hire(self, hire):
        """承認時点での再検証（提案〜承認の間に状態が変わり得るため）。
        上限・id/名前衝突・配属chの既存ホーム重複をここでも弾く。"""
        with db.connect(DB_PATH) as conn:
            existing = db.get_active_agents(conn)
        # 配属先chが既存メンバーのホームと重複するか（二重応答/乗っ取り防止）
        guild = self.get_guild(GUILD_ID)
        ch_name = hr.ai_channel_name(hire["channel_name"])
        ch = (guild.get_channel(int(hire["channel_id"]))
              if hire.get("channel_id") else None)
        if ch is None and guild is not None:
            ch = discord.utils.get(guild.text_channels, name=ch_name)
        taken_homes = ({int(a["home_channel_id"]) for a in AGENTS}
                       | {int(a["home_channel_id"]) for a in existing})
        taken_channel_id = ch.id if (ch and ch.id in taken_homes) else None
        return hr.validate_hire(
            {"new_id": hire["new_id"], "name": hire["name"],
             "role": hire["role"], "channel_name": hire["channel_name"]},
            real_ids={a["id"] for a in AGENTS},
            real_names={a["name"] for a in AGENTS},
            existing_agents=existing, taken_channel_id=taken_channel_id)

    async def _execute_hire(self, payload, hire):
        """承認された採用を実行: 再検証→チャンネル解決/作成→ペルソナ生成→
        台帳登録→再起動なしで反映→完了報告。"""
        proposal_ch = self.get_channel(payload.channel_id)
        hr_name = self._hr_persona_name()
        # 0) 承認時点での再検証（上限・衝突の実行時バイパス防止）
        err = self._revalidate_hire(hire)
        if err:
            with db.connect(DB_PATH) as conn:
                db.set_hire_status(conn, hire["id"], "rejected")
            await webhooks.post_as_persona(
                proposal_ch, name=hr_name, cache=_webhook_cache,
                content=f"⚠️ 承認時点では採用できませんでした: {err}")
            return

        # 手続き中の合図（アイコン生成に数十秒かかるため先に一報）
        try:
            await webhooks.post_as_persona(
                proposal_ch, name=hr_name, cache=_webhook_cache,
                content=f"承認ありがとうございます！**{hire['name']}** の"
                        "準備をしています（アイコンを描いています…30秒ほど）🎨")
        except Exception:
            pass

        guild = self.get_guild(GUILD_ID)
        # 1) 特権ステップ（チャンネル作成＋アイコン生成＋台帳登録＋done）
        try:
            ch_name = hr.ai_channel_name(hire["channel_name"])  # 頭に ai を担保
            channel = None
            if hire["channel_id"]:
                channel = guild.get_channel(int(hire["channel_id"]))
            if channel is None:
                channel = discord.utils.get(guild.text_channels, name=ch_name)
            created = False
            if channel is None:
                # 新規chはAIエージェントカテゴリ配下に作る（設定があれば）
                category = (guild.get_channel(int(AGENT_CATEGORY_ID))
                            if AGENT_CATEGORY_ID else None)
                channel = await guild.create_text_channel(
                    ch_name, category=category)
                created = True
            # アイコン生成（Codex・直列化）。デフォルトアイコンは使わない方針
            async with IMAGE_SEM:
                avatar_rel = await asyncio.to_thread(
                    self._generate_agent_avatar,
                    hire["new_id"], hire["name"], hire["role"])
            persona_rel = os.path.join("personas", f"{hire['new_id']}.md")
            assert re.fullmatch(r"[a-z][a-z0-9_]{1,31}", hire["new_id"])
            with open(_resolve(persona_rel), "w", encoding="utf-8") as f:
                f.write(hr.render_persona(hire["name"], hire["role"]))
            with db.connect(DB_PATH) as conn:
                db.add_agent(
                    conn, id=hire["new_id"], kind="webhook", name=hire["name"],
                    avatar_url=avatar_rel, home_channel_id=channel.id,
                    persona_file=persona_rel,
                    skills_json="{}", allowed_tools_json="[]",
                    created_at=reminders.fmt(reminders.now_jst()),
                    home_channel_created=created)
                db.set_hire_status(conn, hire["id"], "done")
            _load_webhook_agents()  # 再起動なしで反映（avatar_bytesも読込）
            print(f"[hire] hired {hire['new_id']} -> channel {channel.id}"
                  f"{' (created)' if created else ''}"
                  f"{' avatar' if avatar_rel else ' NO-AVATAR'}")
        except discord.Forbidden:
            with db.connect(DB_PATH) as conn:
                db.set_hire_status(conn, hire["id"], "error")
            await webhooks.post_as_persona(
                proposal_ch, name=hr_name, cache=_webhook_cache,
                content="⚠️ チャンネル作成の権限（チャンネルの管理）が無くて"
                        "採用できませんでした。権限をもらえたら再提案します。")
            return
        except Exception as e:
            print(f"[hire] execute failed: {e}")
            with db.connect(DB_PATH) as conn:
                db.set_hire_status(conn, hire["id"], "error")
            return

        # 2) 装飾投稿（あいさつ・完了報告）は別try: 失敗してもdoneは維持。
        #    新エージェントのアイコンは reload 済みの台帳から取る
        new_wa = WEBHOOK_AGENTS.get(hire["new_id"], {})
        avatar_bytes = new_wa.get("avatar_bytes")
        parts = ["（新しい部屋も作りました）"] if created else []
        if not avatar_bytes:
            parts.append("（アイコンは後で作り直します）")
        note = "".join(parts)
        try:
            await webhooks.post_as_persona(
                channel, name=hire["name"], cache=_webhook_cache,
                avatar_bytes=avatar_bytes,
                content=f"はじめまして、{hire['name']}です！"
                        f"{hire['role']}を担当します。よろしくお願いします🙌")
            await webhooks.post_as_persona(
                proposal_ch, name=hr_name, cache=_webhook_cache,
                content=f"✅ 採用しました！**{hire['name']}** を "
                        f"<#{channel.id}> に配属しました{note}。")
        except Exception as e:
            print(f"[hire] post-hire notice failed (already active): {e}")
