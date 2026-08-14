#!/usr/bin/env python3
"""自発性の層（proactive / エージェントv3 Phase A）のユニットテスト。

観察ループの三段ゲート（差分収集・一次判定パース・出典ゲート）と
日次枠の執行を検証する。claude 呼び出しは invoke_fn/search_fn で注入する。
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import proactive

NOW = datetime(2026, 7, 31, 12, 0)
LINK = "https://discord.com/channels/1/2/3"


class ProactiveTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _msg(self, conn, mid, *, ch=1, author=1, text="こんにちは",
             is_bot=False, ch_name="general"):
        db.upsert_channel(conn, id=ch, name=ch_name, type="text")
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name=f"ユーザー{author}", is_bot=is_bot)
        db.insert_message(conn, id=mid, channel_id=ch, author_id=author,
                          content=text, created_at="2026-07-31T11:00:00")

    def _collect(self, **kw):
        args = {"home_channel_id": 100, "exclude_channel_ids": (200,),
                "daily_quota": 3, "now": NOW}
        args.update(kw)
        return proactive.collect_cycle(self.db_path, "agent1", **args)


class CollectCycleTest(ProactiveTestBase):
    def test_first_run_initializes_to_now_and_stays_silent(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 1)
            self._msg(conn, 2)
        self.assertIsNone(self._collect())  # 初回は過去ログを遡らない
        with db.connect(self.db_path) as conn:
            state = db.get_proactive_state(conn, "agent1")
        self.assertEqual(state["last_checked_message_id"], 2)
        self.assertIsNone(self._collect())  # 新規なし → None

    def test_collects_only_new_human_messages(self):
        self._collect()  # 初期化（max=0）
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, ch=1, text="人間の発言")
            self._msg(conn, 11, ch=1, author=2, text="Botの発言", is_bot=True)
            self._msg(conn, 12, ch=200, text="除外chの発言")
            self._msg(conn, 13, ch=100, text="ホームchの発言")
            self._msg(conn, 14, ch=1, text="")  # 空本文（添付のみ等）
        digest = self._collect()
        self.assertEqual([m["id"] for m in digest["messages"]], [10])
        self.assertEqual(digest["messages"][0]["author"], "ユーザー1")
        with db.connect(self.db_path) as conn:
            state = db.get_proactive_state(conn, "agent1")
        # 除外分も含めて checkpoint は最新まで前進する（再処理しない）
        self.assertEqual(state["last_checked_message_id"], 14)

    def test_unknown_author_is_treated_as_bot(self):
        # usersに行が無い投稿者（Webhook等）は人間の発言として扱わない
        self._collect()
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text")
            db.insert_message(conn, id=20, channel_id=1, author_id=999999,
                              content="webhook発言",
                              created_at="2026-07-31T11:00:00")
        self.assertIsNone(self._collect())

    def test_over_limit_advances_checkpoint_partially(self):
        self._collect()  # 初期化
        n = proactive.MAX_MESSAGES_PER_CYCLE
        with db.connect(self.db_path) as conn:
            for i in range(1, n + 2):  # 上限+1件
                self._msg(conn, i, text=f"発言{i}")
        digest = self._collect()
        self.assertEqual(len(digest["messages"]), n)
        with db.connect(self.db_path) as conn:
            state = db.get_proactive_state(conn, "agent1")
        self.assertEqual(state["last_checked_message_id"], n)  # 途中まで
        digest2 = self._collect()  # 残り1件は次周期で消化される
        self.assertEqual([m["id"] for m in digest2["messages"]], [n + 1])

    def test_quota_counts_only_spoke_today(self):
        self._collect()  # 初期化
        with db.connect(self.db_path) as conn:
            db.add_proactive_log(conn, agent_id="agent1", kind="recall",
                                 action="spoke", created_at="2026-07-31T09:00")
            db.add_proactive_log(conn, agent_id="agent1", kind="info",
                                 action="spoke", created_at="2026-07-30T23:59")
            db.add_proactive_log(conn, agent_id="agent1", kind="none",
                                 action="silent",
                                 created_at="2026-07-31T10:00")
            db.add_proactive_log(conn, agent_id="agent2", kind="assist",
                                 action="spoke", created_at="2026-07-31T10:30")
            self._msg(conn, 30, text="新しい発言")
        digest = self._collect(daily_quota=2)
        # 今日のspokeは自分の1件のみ（昨日・silent・他エージェントは数えない）
        self.assertEqual(digest["quota_left"], 1)


class ScreenTest(ProactiveTestBase):
    MSGS = [{"id": 10, "channel_id": 1, "channel": "general",
             "author_id": 1, "author": "かば", "content": "そういえば納期いつだっけ",
             "created_at": "2026-07-31T11:00:00"}]

    def test_prompt_contains_messages_and_types(self):
        prompt = proactive.build_screen_prompt(self.MSGS, "エージェント1")
        self.assertIn("id=10", prompt)
        self.assertIn("納期いつだっけ", prompt)
        for kind in proactive.KINDS:
            self.assertIn(kind, prompt)

    def test_prompt_truncates_long_content(self):
        msgs = [dict(self.MSGS[0], content="あ" * 500)]
        prompt = proactive.build_screen_prompt(msgs, "エージェント1")
        self.assertNotIn("あ" * 400, prompt)

    def test_parse_valid_and_invalid(self):
        raw = ('{"candidates": [{"message_id": 10, "kind": "recall", '
               '"search_terms": ["納期", "締切"], "reason": "疑問"}]}')
        out = proactive.parse_screen_response(raw, {10})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "recall")
        self.assertEqual(out[0]["search_terms"], ["納期", "締切"])
        self.assertEqual(proactive.parse_screen_response(raw, {99}), [])
        self.assertEqual(proactive.parse_screen_response("該当なし", {10}), [])
        bad_kind = raw.replace("recall", "spam")
        self.assertEqual(proactive.parse_screen_response(bad_kind, {10}), [])

    def test_parse_caps_candidates(self):
        c = ('{"message_id": %d, "kind": "recall", "search_terms": ["x"], '
             '"reason": "r"}')
        raw = '{"candidates": [' + ",".join(c % i for i in (1, 2, 3)) + "]}"
        out = proactive.parse_screen_response(raw, {1, 2, 3})
        self.assertEqual(len(out), proactive.MAX_CANDIDATES)

    def test_screen_uses_invoke_fn(self):
        seen = {}

        def fake_invoke(prompt):
            seen["prompt"] = prompt
            return ('{"candidates": [{"message_id": 10, "kind": "recall", '
                    '"search_terms": ["納期"], "reason": "r"}], '
                    '"decisions": [{"message_id": 10, '
                    '"decision": "納期は8/8で確定", "topic": "納期"}]}')

        out = proactive.screen(self.MSGS, agent_name="エージェント1",
                               invoke_fn=fake_invoke)
        self.assertEqual(out["candidates"][0]["message_id"], 10)
        self.assertEqual(out["decisions"][0]["decision"], "納期は8/8で確定")
        self.assertIn("納期いつだっけ", seen["prompt"])

    def test_screen_prompt_asks_for_decisions(self):
        prompt = proactive.build_screen_prompt(self.MSGS, "エージェント1")
        self.assertIn("decisions", prompt)
        self.assertIn("決定", prompt)

    def test_parse_screen_decisions_validates(self):
        raw = ('{"candidates": [], "decisions": ['
               '{"message_id": 10, "decision": "会場はA館で確定", "topic": "会場"},'
               '{"message_id": 99, "decision": "未知idは捨てる"},'
               '{"message_id": 10, "decision": ""}]}')
        out = proactive.parse_screen_decisions(raw, {10})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["decision"], "会場はA館で確定")
        self.assertEqual(proactive.parse_screen_decisions("壊れてる", {10}), [])


class GateReplyTest(unittest.TestCase):
    def test_silent_token(self):
        text, note = proactive.gate_reply("[SILENT]", "recall")
        self.assertIsNone(text)
        self.assertIn("沈黙", note)
        text, _ = proactive.gate_reply("  [SILENT] 理由...", "assist")
        self.assertIsNone(text)

    def test_citation_required_kinds_need_link(self):
        for kind in proactive.CITE_REQUIRED_KINDS:
            text, note = proactive.gate_reply("納期は金曜っス", kind)
            self.assertIsNone(text, kind)
            self.assertIn("出典", note)
            text, _ = proactive.gate_reply(f"納期は金曜っス {LINK}", kind)
            self.assertIsNotNone(text, kind)

    def test_assist_allows_no_link(self):
        text, _ = proactive.gate_reply("こうすると直るっスよ", "assist")
        self.assertEqual(text, "こうすると直るっスよ")

    def test_truncates_overlong_reply(self):
        text, _ = proactive.gate_reply("あ" * 3000 + LINK, "assist")
        self.assertLessEqual(len(text), proactive.MAX_REPLY_CHARS + 1)


class DecideReplyTest(ProactiveTestBase):
    TRIGGER = {"id": 10, "channel_id": 1, "channel": "general",
               "author_id": 1, "author": "かば",
               "content": "そういえば納期いつだっけ",
               "created_at": "2026-07-31T11:00:00"}
    CAND = {"message_id": 10, "kind": "recall",
            "search_terms": ["納期"], "reason": "疑問"}
    HIT = {"id": 5, "channel_id": 2, "channel": "定例", "author": "かば",
           "content": "納期は8/8で確定", "created_at": "2026-07-20T10:00:00",
           "imgs": 0, "vids": 0, "atts": 0}

    def _decide(self, cand=None, search_rows=None, reply="回答"):
        def no_invoke(prompt):
            raise AssertionError("裏付けゼロでclaudeを呼んではいけない")

        invoke_fn = (lambda p: reply) if search_rows else no_invoke
        return proactive.decide_reply(
            self.db_path, "1", "agent1", cand or self.CAND, self.TRIGGER,
            persona="", agent_name="エージェント1",
            invoke_fn=invoke_fn, search_fn=lambda kws: search_rows or [])

    def test_cite_required_without_hits_skips_claude(self):
        text, note = self._decide(search_rows=None)
        self.assertIsNone(text)
        self.assertIn("裏付けなし", note)

    def test_assist_without_hits_still_asks(self):
        cand = dict(self.CAND, kind="assist")
        text, _ = proactive.decide_reply(
            self.db_path, "1", "agent1", cand, self.TRIGGER,
            persona="", agent_name="エージェント1",
            invoke_fn=lambda p: "手伝えるっスよ", search_fn=lambda kws: [])
        self.assertEqual(text, "手伝えるっスよ")

    def test_model_silent_respected(self):
        text, note = self._decide(search_rows=[self.HIT], reply="[SILENT]")
        self.assertIsNone(text)
        self.assertIn("沈黙", note)

    def test_reply_with_citation_passes_gate(self):
        text, _ = self._decide(search_rows=[self.HIT],
                               reply=f"納期は8/8っス {LINK}")
        self.assertIn("8/8", text)

    def test_reply_without_citation_is_silenced(self):
        text, note = self._decide(search_rows=[self.HIT], reply="納期は8/8っス")
        self.assertIsNone(text)
        self.assertIn("出典", note)

    def test_ledger_hit_allows_decide_without_fts(self):
        # FTSゼロでも決定事項台帳にヒットがあれば二次判定に進める（RM#4）
        from core import decisions as decisions_mod
        decisions_mod.save_decisions(
            self.db_path, "agent1",
            [{"decision": "納期は8/8で確定", "topic": "納期"}],
            source_kind="minutes", channel_id=555, source_message_id=42,
            decided_on="2026-07-24")
        seen = {}

        def fake_invoke(prompt):
            seen["prompt"] = prompt
            return "[SILENT]"

        text, note = proactive.decide_reply(
            self.db_path, "1", "agent1", self.CAND, self.TRIGGER,
            persona="", agent_name="エージェント1",
            invoke_fn=fake_invoke, search_fn=lambda kws: [])
        self.assertIsNone(text)               # モデルが沈黙を選んだだけ
        self.assertIn("沈黙", note)            # 裏付けなしバイパスではない
        self.assertIn("決定事項台帳", seen["prompt"])
        self.assertIn("納期は8/8で確定", seen["prompt"])

    def test_prompt_includes_evidence_and_rules(self):
        seen = {}

        def fake_invoke(prompt):
            seen["prompt"] = prompt
            return "[SILENT]"

        with db.connect(self.db_path) as conn:
            db.add_rule(conn, agent_id="agent1", scope="global",
                        rule_text="自発発言は控えめにする",
                        created_by="1", source_msg_id=1,
                        created_at="2026-07-01T00:00")
        proactive.decide_reply(
            self.db_path, "1", "agent1", self.CAND, self.TRIGGER,
            persona="", agent_name="エージェント1",
            invoke_fn=fake_invoke, search_fn=lambda kws: [self.HIT])
        self.assertIn("納期は8/8で確定", seen["prompt"])   # 裏取り材料
        self.assertIn("納期いつだっけ", seen["prompt"])     # 判定対象
        self.assertIn("控えめにする", seen["prompt"])       # 有効ルール注入


class ScopeNoteTest(ProactiveTestBase):
    MSGS = ScreenTest.MSGS

    def test_screen_prompt_uses_scope_note(self):
        prompt = proactive.build_screen_prompt(
            self.MSGS, "エージェント2", scope_note="デザインの話題だけ候補にする")
        self.assertIn("デザインの話題だけ候補にする", prompt)
        self.assertNotIn(proactive.DEFAULT_SCOPE_NOTE, prompt)
        # 未指定なら既定の縄張り文
        prompt = proactive.build_screen_prompt(self.MSGS, "エージェント1")
        self.assertIn(proactive.DEFAULT_SCOPE_NOTE, prompt)

    def test_decide_system_uses_scope_note(self):
        seen = {}

        def fake(prompt):
            seen["called"] = True
            return "[SILENT]"

        proactive.decide_reply(
            self.db_path, "1", "agent2", DecideReplyTest.CAND,
            DecideReplyTest.TRIGGER, persona="", agent_name="エージェント2",
            scope_note="デザインだけ", invoke_fn=fake,
            search_fn=lambda kws: [DecideReplyTest.HIT])
        self.assertTrue(seen["called"])


class QuotaOverrideTest(ProactiveTestBase):
    def test_settings_override_config_default(self):
        self._collect()  # 初期化
        with db.connect(self.db_path) as conn:
            db.set_proactive_quota(conn, "agent1", 0, "2026-07-31T00:00")
            self._msg(conn, 40, text="新着")
        digest = self._collect(daily_quota=3)
        self.assertEqual(digest["quota_left"], 0)  # 上書き0が既定3に勝つ

    def test_quota_marker_extract(self):
        text, reqs = proactive.extract_quota_markers(
            "了解っス！\n[PROACTIVE_QUOTA: agent2 2]")
        self.assertEqual(text, "了解っス！")
        self.assertEqual(reqs, [("agent2", 2)])
        text, reqs = proactive.extract_quota_markers("マーカーなし")
        self.assertEqual(reqs, [])

    def test_apply_and_get_quota(self):
        proactive.apply_quota(self.db_path, "agent2", 5)
        self.assertEqual(proactive.get_quota(self.db_path, "agent2", 1), 5)
        self.assertEqual(proactive.get_quota(self.db_path, "agent3", 1), 1)


class WeeklyReportTest(ProactiveTestBase):
    def test_should_send_only_friday_evening_once(self):
        fri = datetime(2026, 7, 31, 17, 30)  # 金曜
        self.assertTrue(proactive.should_send_weekly_report(
            self.db_path, "agent1", now=fri))
        proactive.mark_weekly_report_sent(self.db_path, "agent1", now=fri)
        self.assertFalse(proactive.should_send_weekly_report(
            self.db_path, "agent1", now=fri))  # 同日2回目は送らない
        self.assertFalse(proactive.should_send_weekly_report(
            self.db_path, "agent1", now=datetime(2026, 7, 30, 18, 0)))  # 木曜
        self.assertFalse(proactive.should_send_weekly_report(
            self.db_path, "agent1", now=datetime(2026, 7, 31, 10, 0)))  # 朝
        self.assertTrue(proactive.should_send_weekly_report(
            self.db_path, "agent1", now=datetime(2026, 8, 7, 17, 5)))  # 翌週

    def test_build_weekly_report(self):
        stats = {"agents": {
            "agent1": {"spoke_by_kind": {"recall": 2, "contradiction": 1},
                      "silent": 14, "nudge": 3, "track": 1,
                      "up": 4, "down": 0}},
            "open_action_items": 5}
        roster = [{"id": "agent1", "name": "エージェント1", "quota": 3},
                  {"id": "agent2", "name": "エージェント2", "quota": 1}]
        text = proactive.build_weekly_report(stats, roster, "07/24")
        self.assertIn("エージェント1: 自発発言3件", text)
        self.assertIn("想起2", text)
        self.assertIn("👍4", text)
        self.assertIn("沈黙判定14回", text)
        self.assertIn("エージェント2: 自発発言0件", text)
        self.assertIn("声かけ3件", text)
        self.assertIn("追跡中タスク5件", text)
        self.assertIn("エージェント1 3回/日・エージェント2 1回/日", text)

    def test_stats_since_aggregates(self):
        with db.connect(self.db_path) as conn:
            db.add_proactive_log(conn, agent_id="agent1", kind="recall",
                                 action="spoke", posted_message_id=100,
                                 created_at="2026-07-30T10:00")
            db.add_proactive_log(conn, agent_id="agent1", kind="none",
                                 action="silent", created_at="2026-07-30T11:00")
            db.add_proactive_log(conn, agent_id="agent1", kind="deadline",
                                 action="nudge", created_at="2026-07-30T12:00")
            db.add_proactive_log(conn, agent_id="agent1", kind="recall",
                                 action="spoke", posted_message_id=101,
                                 created_at="2026-07-01T10:00")  # 期間外
            db.add_feedback(conn, message_id=100, agent_id="agent1",
                            kind="reaction", value="up", user_id="1",
                            created_at="2026-07-30T10:05")
            stats = db.proactive_stats_since(conn, "2026-07-24T00:00")
        s = stats["agents"]["agent1"]
        self.assertEqual(s["spoke_by_kind"], {"recall": 1})
        self.assertEqual(s["silent"], 1)
        self.assertEqual(s["nudge"], 1)
        self.assertEqual(s["up"], 1)


class ReactionLearningTest(ProactiveTestBase):
    """リアクション自動学習（RM#11）: 👎が続いた型の自動抑制。"""

    def _spoke(self, msg_id, *, kind="info", ch=7, created="2026-07-30T10:00"):
        with db.connect(self.db_path) as conn:
            db.add_proactive_log(conn, agent_id="agent1", kind=kind,
                                 action="spoke", channel_id=ch,
                                 posted_message_id=msg_id, created_at=created)

    def _react(self, msg_id, value, user="1"):
        with db.connect(self.db_path) as conn:
            db.add_feedback(conn, message_id=msg_id, agent_id="agent1",
                            kind="reaction", value=value, user_id=user,
                            created_at="2026-07-30T11:00")

    def test_two_downs_suppress_pattern(self):
        self._spoke(101)
        self._spoke(102)
        self._react(101, "down")
        self._react(102, "down", user="2")
        sup = proactive.suppressed_patterns(self.db_path, "agent1", now=NOW)
        self.assertEqual(sup, {("info", 7)})

    def test_single_down_or_up_majority_does_not_suppress(self):
        self._spoke(101)
        self._react(101, "down")            # 👎1件のみ → 過剰反応しない
        self.assertEqual(
            proactive.suppressed_patterns(self.db_path, "agent1", now=NOW),
            set())
        self._spoke(102)
        self._spoke(103)
        self._react(102, "down", user="2")  # 👎2 vs 👍2 → 拮抗は抑制しない
        self._react(101, "up", user="3")
        self._react(103, "up", user="4")
        self.assertEqual(
            proactive.suppressed_patterns(self.db_path, "agent1", now=NOW),
            set())

    def test_old_downs_expire_with_window(self):
        self._spoke(101, created="2026-05-01T10:00")  # 30日窓の外
        self._spoke(102, created="2026-05-01T10:01")
        self._react(101, "down")
        self._react(102, "down", user="2")
        self.assertEqual(
            proactive.suppressed_patterns(self.db_path, "agent1", now=NOW),
            set())

    def test_removing_reaction_lifts_suppression(self):
        self._spoke(101)
        self._spoke(102)
        self._react(101, "down")
        self._react(102, "down", user="2")
        with db.connect(self.db_path) as conn:   # 👎を外す＝解除の口
            db.remove_feedback(conn, message_id=101, user_id="1",
                               value="down")
        self.assertEqual(
            proactive.suppressed_patterns(self.db_path, "agent1", now=NOW),
            set())

    def test_filter_suppressed_drops_only_matching(self):
        msgs = [{"id": 10, "channel_id": 7}, {"id": 11, "channel_id": 8}]
        cands = [{"message_id": 10, "kind": "info"},
                 {"message_id": 11, "kind": "info"},
                 {"message_id": 10, "kind": "recall"}]
        kept, muted = proactive.filter_suppressed(cands, msgs, {("info", 7)})
        self.assertEqual(len(kept), 2)          # 別ch・別類型は残る
        self.assertEqual(muted[0]["channel_id"], 7)
        kept2, muted2 = proactive.filter_suppressed(cands, msgs, set())
        self.assertEqual((len(kept2), muted2), (3, []))

    def test_weekly_report_shows_suppression(self):
        stats = {"agents": {}, "open_action_items": 0}
        roster = [{"id": "agent1", "name": "エージェント1", "quota": 3,
                   "suppressed": 2}]
        text = proactive.build_weekly_report(stats, roster, "07/24")
        self.assertIn("2件", text)
        self.assertIn("自動で控え中", text)
        roster0 = [{"id": "agent1", "name": "エージェント1", "quota": 3,
                    "suppressed": 0}]
        self.assertNotIn("自動で控え中",
                         proactive.build_weekly_report(stats, roster0, "07/24"))


class LessonTest(ProactiveTestBase):
    """自発発言の教訓帳（RM#7）: 👎→自動記録・全解除で撤回・プロンプト注入。"""

    def _spoke_with_message(self, msg_id=200, *, kind="assist", ch=7,
                            text="口出しですみません、これはこうすべきです"):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=ch, name="general", type="text")
            db.upsert_user(conn, id=999, name="agent1", display_name="エージェント1",
                           is_bot=True)
            db.insert_message(conn, id=msg_id, channel_id=ch, author_id=999,
                              content=text, created_at="2026-07-31T12:00:00")
            db.add_proactive_log(conn, agent_id="agent1", kind=kind,
                                 action="spoke", channel_id=ch,
                                 posted_message_id=msg_id,
                                 created_at="2026-07-31T12:00")

    def test_down_records_lesson_once(self):
        self._spoke_with_message()
        self.assertEqual(
            proactive.record_lesson_from_feedback(self.db_path, 200),
            "assist")
        # 2人目の👎では重複記録しない
        self.assertIsNone(
            proactive.record_lesson_from_feedback(self.db_path, 200))
        with db.connect(self.db_path) as conn:
            lessons = db.recent_proactive_lessons(conn, "agent1")
        self.assertEqual(len(lessons), 1)
        self.assertIn("口出しですみません", lessons[0]["text"])

    def test_non_proactive_message_is_ignored(self):
        self.assertIsNone(
            proactive.record_lesson_from_feedback(self.db_path, 12345))

    def test_lift_when_all_downs_removed(self):
        self._spoke_with_message()
        proactive.record_lesson_from_feedback(self.db_path, 200)
        with db.connect(self.db_path) as conn:
            db.add_feedback(conn, message_id=200, agent_id="agent1",
                            kind="reaction", value="down", user_id="1",
                            created_at="t")
        # 👎がまだ残っている間は解除しない
        self.assertFalse(proactive.lift_lesson_if_no_downs(self.db_path, 200))
        with db.connect(self.db_path) as conn:
            db.remove_feedback(conn, message_id=200, user_id="1",
                               value="down")
        self.assertTrue(proactive.lift_lesson_if_no_downs(self.db_path, 200))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.recent_proactive_lessons(conn, "agent1"), [])

    def test_lessons_block_and_prompt_injection(self):
        self._spoke_with_message()
        proactive.record_lesson_from_feedback(self.db_path, 200)
        block = proactive.build_lessons_block(
            [{"kind": "assist", "text": "口出しですみません"}])
        self.assertIn("過去の教訓", block)
        self.assertIn("②", block)
        self.assertEqual(proactive.build_lessons_block([]), "")
        # decide_reply のプロンプトに注入されること
        seen = {}

        def fake_invoke(prompt):
            seen["prompt"] = prompt
            return "[SILENT]"

        proactive.decide_reply(
            self.db_path, "1", "agent1", DecideReplyTest.CAND,
            DecideReplyTest.TRIGGER, persona="", agent_name="エージェント1",
            invoke_fn=fake_invoke,
            search_fn=lambda kws: [DecideReplyTest.HIT])
        self.assertIn("口出しですみません", seen["prompt"])


class LogEntryTest(ProactiveTestBase):
    def test_log_and_count(self):
        proactive.log_entry(self.db_path, "agent1", kind="recall",
                            action="spoke", channel_id=1,
                            trigger_message_id=10, posted_message_id=20,
                            detail="test")
        with db.connect(self.db_path) as conn:
            n = db.count_proactive_spoken_since(conn, "agent1", "2000-01-01")
            rows = conn.execute(
                "SELECT kind, action, posted_message_id FROM proactive_log"
            ).fetchall()
        self.assertEqual(n, 1)
        self.assertEqual(rows[0], ("recall", "spoke", 20))


if __name__ == "__main__":
    unittest.main()
