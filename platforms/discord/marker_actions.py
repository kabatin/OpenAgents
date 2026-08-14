#!/usr/bin/env python3
"""マーカー実行（RULE / REMIND / PROACTIVE_QUOTA など）。

bot.py から分離した AgentClient の mixin。LLM出力のマーカーを検証つきで
実行し、実挙動を -# 行で明示する（LLMの「登録しました」と実際のズレを見せる）。

ここに足すのは**組み込みのマーカー**だけ。外部サービスを叩くマーカーは
integrations/ 側に書く（integrations.py の apply_markers フックを使う）。"""

from core import action_items
from core import db
from core import glossary
from core import honesty
from core import proactive
from core import reminders
from core import rules
import json
from platforms.discord.agent_runtime import (
    ADMIN_IDS,
    AGENTS_BY_ID,
    DB_PATH,
    _can_broadcast,
    _is_broadcast,
    can_target_channel,
    resolve_channel,
    resolve_mentions,
    split_channel_tokens,
)

class MarkerActionsMixin:
    """AgentClient に混ぜる mixin（self.* は bot.py の属性・設定を参照する）。"""

    def _apply_rule_markers(self, message, answer, agent_id=None):
        """回答中の RULE / RULE_CANCEL / CAPABILITY マーカーを実行し、
        本文＋実行結果の -# 行を返す（マーカー自体は必ず除去）。
        リマインダーと同様、実挙動を -# 行で明示する。
        agent_id 未指定なら自分（本物Bot）、指定時はWebhook人格として保存する。"""
        aid = agent_id or self.agent["id"]
        text, adds, cancel_ids, caps, errors = rules.extract_markers(answer)
        notes = []
        now_dt = reminders.now_jst()
        now = reminders.fmt(now_dt)
        author_id = str(message.author.id)
        is_admin = author_id in ADMIN_IDS
        with db.connect(DB_PATH) as conn:
            for req in adds:
                # global（全ch共通）は影響範囲が広いため管理者のみ
                if req["scope"] == "global" and not is_admin:
                    notes.append("-# ⚠️ 全体共通ルールは管理者だけが設定できる"
                                 "っス（このチャンネル/あなた向けなら設定可）")
                    continue
                scope = rules.scope_key(
                    req["scope"], channel_id=message.channel.id,
                    user_id=message.author.id)
                expires_at = rules.expiry_from(req.get("duration"), now_dt)
                rid = db.add_rule(
                    conn, agent_id=aid, scope=scope,
                    rule_text=req["text"], created_by=author_id,
                    source_msg_id=message.id, created_at=now,
                    expires_at=expires_at)
                exp = rules.expiry_label(expires_at)
                notes.append(f"-# 📌 ルール登録(id={rid}, "
                             f"{rules.scope_label(scope)}){exp}: "
                             f"{req['text'][:60]}")
            for rid in cancel_ids:
                r = db.get_rule(conn, rid, aid)
                if r is None:
                    notes.append(f"-# ⚠️ id={rid} のルールは見つからないっス")
                elif not is_admin and r["created_by"] != author_id:
                    # 自分が作ったルール以外は管理者しか消せない
                    notes.append(f"-# ⚠️ id={rid} は他の人/共有のルールなので"
                                 "削除できないっス（管理者に相談を）")
                else:
                    db.deactivate_rule(conn, rid, aid)
                    notes.append(f"-# 🗑 ルール削除(id={rid}): "
                                 f"{r['rule_text'][:50]}")
            for desc in caps:
                cid = db.add_capability_request(
                    conn, agent_id=aid, description=desc,
                    context=(message.clean_content or "")[:500],
                    requested_by=str(message.author.id),
                    source_msg_id=message.id, created_at=now)
                notes.append(f"-# 🧩 能力追加を起票(id={cid}): {desc[:60]}")
        for e in errors:
            notes.append(f"-# ⚠️ ルール登録に失敗: {e}")
        if not notes:
            return text
        return (text + "\n" if text else "") + "\n".join(notes)

    def _apply_quota_markers(self, message, answer):
        """回答中の PROACTIVE_QUOTA マーカーを実行（Phase D・必ず除去）。
        枠の変更は管理者のみ・対象idと上限をコードで検証（LLMに委ねない）。"""
        text, reqs = proactive.extract_quota_markers(answer)
        if not reqs:
            return answer  # マーカー無しなら本文に手を入れない
        notes = []
        is_admin = str(message.author.id) in ADMIN_IDS
        for agent_id, quota in reqs:
            target = AGENTS_BY_ID.get(agent_id)
            if not is_admin:
                notes.append("-# ⚠️ 自発発言の枠は管理者だけが変更できるっス")
            elif target is None or quota > proactive.QUOTA_MAX:
                notes.append(f"-# ⚠️ 枠の指定が不正っス（{agent_id} {quota}）")
            else:
                proactive.apply_quota(DB_PATH, agent_id, quota)
                notes.append(f"-# ⚙️ {target['name']}の自発発言枠を"
                             f"{quota}回/日に変更したっス（次の観察周期から）")
        if not notes:
            return text
        return (text + "\n" if text else "") + "\n".join(notes)

    def _apply_reminder_markers(self, message, answer):
        """回答中の REMIND / REMIND_CANCEL マーカーを実行し、
        本文＋実行結果の -# 行を返す（マーカー自体は必ず除去）。
        LLM本文の「登録しました」と実挙動のズレに気づけるよう、
        成功も失敗も -# 行で明示する。"""
        text, adds, cancel_ids, errors = reminders.extract_markers(answer)
        notes = []
        terms_rows = None
        for req in adds:
            mention = label = None
            deliver_channel_id = message.channel.id
            channel_label = None
            to_people = req["to"]
            if req["to"]:
                # チャンネル宛（起票#4）: #名前 / <#id> を人宛より先に分離する
                chan_tokens, to_people = split_channel_tokens(req["to"])
                if chan_tokens:
                    target = resolve_channel(message.guild, chan_tokens[0])
                    if target is None:
                        notes.append(f"-# ⚠️ チャンネル「{chan_tokens[0]}」が"
                                     "見つからないため、このチャンネルに"
                                     "通知するっス")
                    elif not can_target_channel(target, message.author):
                        notes.append("-# ⚠️ あなたが書き込めないチャンネル"
                                     "には設定できないっス。このチャンネルに"
                                     "通知するっス")
                    else:
                        deliver_channel_id = target.id
                        channel_label = f"#{target.name}"
                    if len(chan_tokens) > 1:
                        notes.append("-# ⚠️ チャンネル指定は最初の1つ"
                                     f"（{chan_tokens[0]}）だけ使ったっス")
            if to_people:
                if terms_rows is None:
                    with db.connect(DB_PATH) as conn:
                        terms_rows = db.terms_all(conn)
                mention, label, unresolved = resolve_mentions(
                    message.guild, to_people, terms_rows)
                if mention is None:
                    notes.append(f"-# ⚠️ 宛先「{to_people}」が見つからないか"
                                 "同名が複数いるため依頼者宛にしたっス")
                elif unresolved:
                    # 一部だけ解決できた: 解決分で登録し、残りを明示する
                    notes.append("-# ⚠️ 宛先のうち「"
                                 + "、".join(unresolved)
                                 + "」は見つからなかったっス"
                                 f"（{label} には届くっス）")
                if mention is not None and (_is_broadcast(mention)
                        and not _can_broadcast(message.author)):
                    # LLMの裁量に任せず、発言者のDiscord権限で強制ゲート
                    notes.append("-# ⚠️ 全員/ロール宛はメンション権限を持つ人"
                                 "だけ設定できるっス。依頼者宛にしたっス")
                    mention = label = None
            entry, err = reminders.add_reminder(
                channel_id=deliver_channel_id,
                user_id=message.author.id,
                user_name=message.author.display_name,
                content=req["content"],
                due=req["due"],
                repeat=req["repeat"],
                agent_id=self.agent["id"],
                mention=mention,
                mention_label=label,
                channel_label=channel_label,
            )
            if entry:
                notes.append("-# 登録: " + reminders.format_entry_line(entry))
            else:
                notes.append(f"-# ⚠️ 登録できなかったっス: {err}")
        is_admin = str(message.author.id) in ADMIN_IDS
        for rid in cancel_ids:
            entry, reason = reminders.cancel_reminder(
                rid, message.author.id, is_admin=is_admin)
            if entry:
                owner_note = ""
                if entry["user_id"] != str(message.author.id):
                    owner_note = (f"（{entry['user_name']}さんの分・"
                                  "管理者権限で取消）")
                notes.append(f"-# キャンセル: id={rid} "
                             f"{entry['content'][:40]}{owner_note}")
            elif reason == "not_found":
                notes.append(f"-# ⚠️ id={rid} は存在しないっス")
            elif reason == "ended":
                old = reminders.find_entry(rid)
                label = {"done": "配信済み", "cancelled": "取消済み"}.get(
                    (old or {}).get("status"), "終了済み")
                notes.append(f"-# ⚠️ id={rid} は既に{label}っス")
            elif reason == "not_owner":
                old = reminders.find_entry(rid)
                owner = (old or {}).get("user_name") or "他の人"
                notes.append(f"-# ⚠️ id={rid} は{owner}さんのリマインダー"
                             "なので、本人か管理者しか取り消せないっス")
        for e in errors:
            notes.append(f"-# ⚠️ 登録できなかったっス: {e}")
        if not notes:
            return text
        return (text + "\n" if text else "") + "\n".join(notes)

    def _apply_action_markers(self, message, answer):
        """回答中の ACTION_CANCEL / ACTION_DONE マーカーを実行し、
        本文＋実行結果の -# 行を返す（マーカー自体は必ず除去）。
        会話で「不要になった」と言われた追跡タスクを、リアクション（✅/❌）と
        同じ実挙動で終了させる（実例: 口約束だけでアラートが出続けた）。"""
        text, cancel_ids, done_ids = \
            action_items.extract_conversation_markers(answer)
        if not cancel_ids and not done_ids:
            return answer
        notes, applied = action_items.apply_conversation_ops(
            DB_PATH, self.agent["id"],
            author_id=str(message.author.id),
            is_admin=str(message.author.id) in ADMIN_IDS,
            cancel_ids=cancel_ids, done_ids=done_ids)
        for op in applied:
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="deadline",
                action=op["action"], channel_id=message.channel.id,
                trigger_message_id=message.id, detail=op["task"][:60])
        if not notes:
            return text
        return (text + "\n" if text else "") + "\n".join(notes)

    def _apply_honesty_check(self, message, answer, hits):
        """「できたフリ」検出（RM#20）。①完了主張×マーカー不発→正直化の-#行を
        付記＋記録 ②根拠なし断定→シャドー記録のみ（本文は触らない）。"""
        # そのスキルを持たないエージェントでは検査自体をしない（誤検出防止）。
        # 例: 納期追跡を持たないエージェントの「追跡はキャンセルするっス」は
        # ただの日常会話であって、できたフリではない。
        # 外部連携が honesty.register() した kind も、その連携が有効な
        # エージェントでだけ検査する
        enabled = {i.skill_key for i in getattr(self, "integrations", ())}
        skip = tuple(kind for kind in honesty.CLAIMS
                     if (kind == "action"
                         and not getattr(self, "action_tracking", False))
                     or (kind not in honesty.BUILTIN_KINDS
                         and kind not in enabled))
        # 失敗した時は「できました」と言わせない: 嘘の完了主張の文を本文から
        # 消し、1行目に失敗を置く（人間は1行目を読んで判断する。矛盾した
        # 2つの文を並べて読み手に解決させるのは誤り）
        missing = honesty.detect_fake_done(answer, skip=skip)
        if missing:
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="fake_done", action="caught",
                channel_id=message.channel.id, trigger_message_id=message.id,
                detail=",".join(missing))
            print(f"[{self.agent['id']}] fake-done caught: {missing}")
            return self._honest_failure(
                honesty.build_fake_done_note(missing), answer, missing)
        # 完了主張×実行結果が失敗のみ＝本文と実挙動の矛盾
        # （例: APIキーのスコープ不足で403なのに「削除しときました」）
        failed = honesty.detect_failed_claim(answer, skip=skip)
        if failed:
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="fake_done",
                action="failed_claim", channel_id=message.channel.id,
                trigger_message_id=message.id, detail=",".join(failed))
            print(f"[{self.agent['id']}] failed-claim caught: {failed}")
            return self._honest_failure(
                honesty.build_failed_claim_note(failed), answer, failed)
        if honesty.unsourced_assertion(answer, hits):
            # 幻覚自己検出（RM#85）: 社内ログで自動裏取りし、裏付けの無い断定には
            # 自信度低めの自己申告を強制付記する（#83の自己申告のコード執行版）
            verified, ex = honesty.verify_assertion(DB_PATH, answer)
            action = "assert_verified" if verified else "assert_flagged"
            if not verified:
                answer = ((answer + "\n" if answer else "")
                          + "-# 🤔 自信度低め: 上記の断定は社内ログで"
                          "裏取りできませんでした（要確認）")
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="fake_done",
                action=action, channel_id=message.channel.id,
                trigger_message_id=message.id, detail=ex[:200])
        return answer

    @staticmethod
    def _honest_failure(note, answer, kinds):
        """失敗の告知を1行目に置き、嘘の完了主張を本文から取り除いて返す。"""
        rest = honesty.strip_claims(answer, kinds)
        return note + ("\n\n" + rest if rest else "")

    def _apply_glossary_markers(self, message, answer):
        """回答中の GLOSSARY マーカーを実行（RM#5・必ず除去）。
        登録は誰でも可（表記の訂正は低リスク）・削除は管理者のみ。
        登録時は決定台帳・納期タスク等の伝染済み表記も遡及修正する。"""
        text, adds, cancels, errors = glossary.extract_markers(answer)
        text, term_adds, term_cancels, term_errors = \
            glossary.extract_term_markers(text)
        errors += term_errors
        if not any((adds, cancels, term_adds, term_cancels, errors)):
            return answer
        notes = []
        is_admin = str(message.author.id) in ADMIN_IDS
        for t in term_adds:
            glossary.save_term(DB_PATH, t["term"], t["description"],
                               str(message.author.id))
            desc = f"（{t['description']}）" if t["description"] else ""
            notes.append(f"-# 📛 固有名詞を登録: 「{t['term']}」{desc}"
                         "（議事録・回答で正式表記として扱うっス）")
        for term in term_cancels:
            if not is_admin:
                notes.append("-# ⚠️ 固有名詞の削除は管理者だけっス")
            elif glossary.remove_term(DB_PATH, term):
                notes.append(f"-# 🗑 固有名詞辞書から削除: 「{term}」")
            else:
                notes.append(f"-# ⚠️ 「{term}」は辞書に無いっス")
        for wrong, correct in adds:
            fixed = glossary.save(DB_PATH, wrong, correct,
                                  str(message.author.id))
            note = f"-# 📖 単語帳に登録: 「{wrong}」→「{correct}」"
            note += "（今後の議事録・回答・検索に反映"
            if fixed:
                note += f"・台帳{fixed}件も修正"
            note += "）"
            notes.append(note)
        for wrong in cancels:
            if not is_admin:
                notes.append("-# ⚠️ 単語帳の削除は管理者だけっス")
            elif glossary.remove(DB_PATH, wrong):
                notes.append(f"-# 🗑 単語帳から削除: 「{wrong}」")
            else:
                notes.append(f"-# ⚠️ 「{wrong}」は単語帳に無いっス")
        for e in errors:
            notes.append(f"-# ⚠️ {e}")
        if not notes:
            return text
        return (text + "\n" if text else "") + "\n".join(notes)
