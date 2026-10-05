#!/usr/bin/env python3
"""とっておきパック（複数宛先リマインダー／#101波及／#102浦島／#103Wiki）のテスト。"""

import os
import tempfile
import unittest

from platforms.discord import agent_runtime
from core import comeback
from core import db
from core import ripple
from core import wiki


class FakeMember:
    def __init__(self, name, display_name, uid):
        self.name = name
        self.display_name = display_name
        self.mention = f"<@{uid}>"


class FakeGuild:
    def __init__(self, members):
        self.members = members
        self.roles = []


TERMS = [
    {"term": "山田", "description":
     "Discord ID: yamada、別名: やまちゃん、山田さん、ヤマダ"},
    {"term": "田中", "description":
     "Discord ID: tanaka_pr、別名: 田中さん、たなちゃん、田中（広報）"},
]
GUILD = FakeGuild([
    FakeMember("yamada", "山田", 100000000000000002),
    FakeMember("tanaka_pr", "田中（広報）", 100000000000000003),
    FakeMember("tanaka2", "田中", 100000000000000004),
])


class MultiMentionTest(unittest.TestCase):
    def test_terms_username_resolves_name_and_alias(self):
        self.assertEqual(agent_runtime._terms_username("山田", TERMS),
                         "yamada")
        self.assertEqual(agent_runtime._terms_username("たなちゃん", TERMS),
                         "tanaka_pr")
        self.assertIsNone(agent_runtime._terms_username("知らない人", TERMS))

    def test_multiple_recipients_comma(self):
        mention, label, unresolved = agent_runtime.resolve_mentions(
            GUILD, "山田,田中", TERMS)
        self.assertEqual(mention,
                         "<@100000000000000002> <@100000000000000003>")
        self.assertEqual(label, "@山田 @田中")
        self.assertEqual(unresolved, [])

    def test_dictionary_wins_over_same_display_name(self):
        """表示名「田中」の別人がいても、対応表のtanaka_prに解決する。"""
        mention, _label, _ = agent_runtime.resolve_mentions(
            GUILD, "田中", TERMS)
        self.assertEqual(mention, "<@100000000000000003>")

    def test_without_terms_falls_back_to_guild(self):
        mention, _label, _ = agent_runtime.resolve_mentions(
            GUILD, "田中", [])
        self.assertEqual(mention, "<@100000000000000004>")   # 表示名一致

    def test_partial_resolution_reports_unresolved(self):
        mention, label, unresolved = agent_runtime.resolve_mentions(
            GUILD, "山田、存在しない人", TERMS)
        self.assertEqual(mention, "<@100000000000000002>")
        self.assertEqual(unresolved, ["存在しない人"])

    def test_none_resolved(self):
        mention, label, unresolved = agent_runtime.resolve_mentions(
            GUILD, "誰それ,某氏", TERMS)
        self.assertIsNone(mention)
        self.assertEqual(len(unresolved), 2)

    def test_duplicate_names_deduped(self):
        mention, _l, _ = agent_runtime.resolve_mentions(
            GUILD, "山田,やまちゃん", TERMS)
        self.assertEqual(mention, "<@100000000000000002>")

    def test_broadcast_detection_in_joined(self):
        self.assertTrue(agent_runtime._is_broadcast("<@1> <@&5>"))
        self.assertFalse(agent_runtime._is_broadcast("<@1> <@2>"))


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class RippleTest(TestBase):
    def _decision(self, text, topic="イベント"):
        with db.connect(self.db_path) as conn:
            return db.add_decision(
                conn, agent_id="agent1", decision=text, topic=topic,
                source_kind="minutes", source_message_id=1, channel_id=7,
                decided_on="2026-08-01", created_at="t")

    def test_impacts_validated_against_candidates(self):
        old = self._decision("サマーカップは8/29開催で確定")
        new_id = self._decision("サマーカップは9/5に延期で確定")
        nd = {"id": new_id, "decision": "サマーカップは9/5に延期で確定",
              "channel_id": 7}
        cands = ripple.gather_candidates(self.db_path, nd)
        self.assertIn(old, [d["id"] for d in cands["decisions"]])
        impacts = ripple.parse_impacts(
            f'{{"impacts": [{{"kind": "decision", "id": {old}, '
            f'"why": "開催日が矛盾"}}, '
            '{"kind": "decision", "id": 999, "why": "捏造id"}]}', cands)
        self.assertEqual(len(impacts), 1)   # 捏造idは捨てる
        self.assertEqual(impacts[0]["id"], old)

    def _task(self, task, due, owners="<@111>"):
        with db.connect(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO users(id, display_name) VALUES(111, '田中')")
            return db.add_action_item(
                conn, agent_id="agent1", source_message_id=1, channel_id=7,
                task=task, owners=owners, due_date=due, urgent=0, created_at="t")

    def test_only_changes_are_actionable(self):
        # 一致・具体化しただけのものは投稿しない（いつも放置される原因だった）
        impacts = [{"kind": "action_item", "id": 1, "effect": "same", "why": "一致"},
                   {"kind": "action_item", "id": 2, "effect": "related", "why": "関連"}]
        self.assertEqual(ripple.actionable(impacts), [])
        moved = {"kind": "action_item", "id": 3, "effect": "reschedule",
                 "new_due": "2026-10-15", "why": "定例が木曜に"}
        self.assertEqual(ripple.actionable(impacts + [moved]), [moved])

    def test_parse_keeps_effect_and_valid_due(self):
        tid = self._task("定例の準備", "2026-10-16")
        nd = {"id": self._decision("定例は10/15から木曜開催"),
              "decision": "定例は10/15から木曜開催", "channel_id": 7}
        cands = ripple.gather_candidates(self.db_path, nd)
        impacts = ripple.parse_impacts(
            f'{{"impacts": [{{"kind": "action_item", "id": {tid}, '
            '"effect": "reschedule", "new_due": "2026-10-15", "why": "木曜開催に"}, '
            f'{{"kind": "action_item", "id": {tid}, "effect": "reschedule", '
            '"new_due": "来週", "why": "日付でない"}]}', cands)
        self.assertEqual(impacts[0]["new_due"], "2026-10-15")
        self.assertEqual(impacts[0]["old_due"], "2026-10-16")
        self.assertEqual(impacts[0]["label"], "定例の準備")
        self.assertEqual(impacts[0]["owner"], "田中")
        self.assertEqual(impacts[1]["effect"], "related")   # 日付でなければ自動では変えない

    def test_conflict_only_for_decisions(self):
        tid = self._task("定例の準備", "2026-10-16")
        nd = {"id": self._decision("定例は木曜開催"), "decision": "定例は木曜開催",
              "channel_id": 7}
        cands = ripple.gather_candidates(self.db_path, nd)
        impacts = ripple.parse_impacts(
            f'{{"impacts": [{{"kind": "action_item", "id": {tid}, '
            '"effect": "conflict", "why": "w"}]}', cands)
        self.assertEqual(impacts[0]["effect"], "related")

    def test_proposal_says_what_changes_and_what_check_means(self):
        text = ripple.build_proposal(
            {"decision": "定例は10/15から木曜開催"},
            [{"kind": "action_item", "id": 18, "effect": "reschedule",
              "label": "定例の準備", "owner": "田中", "old_due": "2026-10-16",
              "new_due": "2026-10-15", "why": "定例が木曜開催になるため"},
             {"kind": "decision", "id": 33, "effect": "conflict",
              "label": "定例は金曜開催", "why": "曜日が食い違う"},
             {"kind": "reminder", "id": 14, "effect": "reschedule",
              "label": "定例のリマインド", "old_due": "2026-10-16T09:00",
              "why": "日付がずれる"}])
        self.assertIn("A18「定例の準備」", text)
        self.assertIn("田中", text)
        self.assertIn("10/16 → **10/15**", text)
        self.assertIn("決定#33「定例は金曜開催」", text)
        self.assertIn("自動では直せない", text)        # 案を作れないものは明示する
        self.assertIn("返信で直し方を教えて", text)   # 直し方をエージェントの手元で教わる
        self.assertIn("✅＝", text)
        self.assertIn("❌＝", text)
        self.assertNotIn("っス", text)
        self.assertNotIn("教訓", text)   # 覚えていないのに「教訓として覚える」と言わない

    def test_approve_applies_due_change_and_supersede(self):
        old = self._decision("定例は金曜開催")
        new_id = self._decision("定例は10/15から木曜開催")
        tid = self._task("定例の準備", "2026-10-16")
        pid = ripple.register(self.db_path, new_id, [
            {"kind": "decision", "id": old, "effect": "conflict", "why": "w"},
            {"kind": "action_item", "id": tid, "effect": "reschedule",
             "new_due": "2026-10-15", "why": "w"}])
        ripple.set_message(self.db_path, pid, 500)
        self.assertEqual(ripple.approve(self.db_path, 500),
                         {"decisions": 1, "dues": 1, "reminders": 0})
        with db.connect(self.db_path) as conn:
            st = conn.execute("SELECT status FROM decisions WHERE id=?", (old,)).fetchone()[0]
            due = conn.execute("SELECT due_date FROM action_items WHERE id=?", (tid,)).fetchone()[0]
        self.assertEqual((st, due), ("superseded", "2026-10-15"))
        self.assertIsNone(ripple.approve(self.db_path, 500))   # CAS

    def _weekly_reminder(self):
        from datetime import datetime
        from core import reminders
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = reminders.STATE_FILE
        reminders.STATE_FILE = os.path.join(tmp.name, "reminders.json")
        self.addCleanup(lambda: setattr(reminders, "STATE_FILE", orig))
        e, _ = reminders.add_reminder("1", "2", "担当者", "毎週金曜日は定例です。",
                                      datetime(2099, 10, 9, 18, 0), "weekly")
        return e

    def test_reminder_shift_is_proposed_and_applied(self):
        # 「定例は木曜開催に」→ 毎週金曜のリマインドの直し方まで作って✅で反映する
        rem = self._weekly_reminder()
        nd = {"id": self._decision("定例は木曜開催"), "decision": "定例は木曜開催",
              "channel_id": 7}
        cands = ripple.gather_candidates(self.db_path, nd)
        impacts = ripple.parse_impacts(
            f'{{"impacts": [{{"kind": "reminder", "id": {rem["id"]}, '
            '"effect": "reschedule", "why": "木曜開催に", '
            '"shift": {"weekday": 3, "start": "2099-10-15", "end": "2099-11-12", '
            '"content": "今週は木曜日が定例です。"}}]}', cands)
        self.assertIsNotNone(impacts[0]["plan"])
        text = ripple.build_proposal(nd, impacts)
        self.assertIn("（臨時）", text)
        self.assertIn("✅＝1 をそのまま反映する", text)
        pid = ripple.register(self.db_path, nd["id"], impacts)
        ripple.set_message(self.db_path, pid, 777)
        done = ripple.approve(self.db_path, 777)
        self.assertEqual(done["reminders"], 1)
        self.assertIn("リマインダー1件", ripple.applied_note(done))

    def test_temporary_without_end_asks_for_end(self):
        rem = self._weekly_reminder()
        nd = {"id": self._decision("定例は一時的に木曜開催"),
              "decision": "定例は一時的に木曜開催", "channel_id": 7}
        cands = ripple.gather_candidates(self.db_path, nd)
        impacts = ripple.parse_impacts(
            f'{{"impacts": [{{"kind": "reminder", "id": {rem["id"]}, '
            '"effect": "reschedule", "why": "w", "shift": {"weekday": 3, '
            '"start": "2099-10-15", "end": null, "temporary": true}}]}', cands)
        self.assertIn("戻す日が決まったら教えて", ripple.build_proposal(nd, impacts))

    def test_applied_note_reports_what_was_done(self):
        self.assertEqual(ripple.applied_note({"decisions": 1, "dues": 2}),
                         "タスク2件の期日を直し、古い決定1件を上書き済みにしました")
        self.assertEqual(ripple.applied_note({"decisions": 0, "dues": 0}),
                         "確認済みにしました（自動で直したものはありません）")

    def test_dismiss(self):
        new_id = self._decision("新決定")
        pid = ripple.register(self.db_path, new_id, [])
        ripple.set_message(self.db_path, pid, 501)
        self.assertTrue(ripple.dismiss(self.db_path, 501))
        self.assertFalse(ripple.dismiss(self.db_path, 501))


class ComebackTest(TestBase):
    def _msg(self, mid, uid, created, ch=1, bot=False):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=ch, name="g", type="text")
            db.upsert_user(conn, id=uid, name=f"u{uid}",
                           display_name=f"人{uid}", is_bot=bot)
            db.insert_message(conn, id=mid, channel_id=ch, author_id=uid,
                              content="x", created_at=created)

    def test_first_scan_only_initializes(self):
        self._msg(10, 1, "2026-08-01T10:00")
        self.assertEqual(comeback.detect(self.db_path, "agent1"), [])

    def test_detects_comeback_after_gap(self):
        self._msg(10, 1, "2026-07-20T10:00")
        comeback.detect(self.db_path, "agent1")          # 初期化
        self._msg(50, 1, "2026-08-02T09:00")            # 13日ぶり
        found = comeback.detect(self.db_path, "agent1")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["user_id"], 1)
        self.assertEqual(found[0]["days"], 12)   # 7/20 10:00→8/2 9:00は丸12日
        # 直後の発言では二重welcomeしない（クールダウン）
        comeback.mark_welcomed(self.db_path, 1)
        self._msg(51, 1, "2026-08-02T09:05")
        self.assertEqual(comeback.detect(self.db_path, "agent1"), [])

    def test_short_gap_ignored(self):
        self._msg(10, 1, "2026-08-01T10:00")
        comeback.detect(self.db_path, "agent1")
        self._msg(50, 1, "2026-08-02T09:00")            # 1日
        self.assertEqual(comeback.detect(self.db_path, "agent1"), [])

    def test_digest_contains_absence_events(self):
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1", decision="値段は3000円",
                            topic="t", source_kind="chat",
                            source_message_id=1, channel_id=1,
                            decided_on="2026-07-25",
                            created_at="2026-07-25T10:00")
            db.add_action_item(conn, agent_id="agent1", source_message_id=2,
                               channel_id=1, task="バナー作成",
                               owners="<@1>", due_date="2026-08-10",
                               urgent=False, created_at="2026-07-26T10:00")
        lines = comeback.build_digest(self.db_path, 1, "2026-07-20T10:00")
        joined = "\n".join(lines)
        self.assertIn("値段は3000円", joined)
        self.assertIn("あなた宛タスク", joined)
        self.assertIsNone(comeback.build_digest(self.db_path, 99,
                                                "2026-08-02T00:00")
                          if not lines else None)

    def test_post_text(self):
        post = comeback.build_post(13, ["- [決定] x"])
        self.assertIn("13日ぶり", post)
        self.assertIn("1回だけ", post)


class WikiTest(TestBase):
    def test_extract_markers(self):
        text, topics = wiki.extract_markers(
            "作ります！\n[WIKI: サマーカップ]\n[WIKI: サマーカップ]")
        self.assertEqual(text, "作ります！")
        self.assertEqual(topics, ["サマーカップ"])

    def test_compile_needs_material(self):
        def boom(p):
            raise AssertionError("材料ゼロで呼んだ")
        self.assertIsNone(wiki.compile_page(
            self.db_path, "無い話題", 123, model="x", invoke_fn=boom))

    def test_compile_includes_sources(self):
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1",
                            decision="サマーカップは8/29開催",
                            topic="サマーカップ", source_kind="minutes",
                            source_message_id=42, channel_id=7,
                            decided_on="2026-07-20", created_at="t")
        captured = {}
        def fake(p):
            captured["prompt"] = p
            return "📖 **サマーカップ**\n- 8/29開催"
        body = wiki.compile_page(self.db_path, "サマーカップ", 123,
                                 model="x", invoke_fn=fake,
                                 today="2026-08-02")
        self.assertIn("8/29開催", body)
        self.assertIn("discord.com/channels/123/7/42", captured["prompt"])

    def test_update_precheck_is_deterministic(self):
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1",
                            decision="サマーカップは8/29開催",
                            topic="大会", source_kind="minutes",
                            source_message_id=1, channel_id=7,
                            decided_on="2026-07-20", created_at="t")
        wiki.save_page(self.db_path, topic="サマーカップ", channel_id=7,
                       message_id=100, created_by=1)
        self.assertEqual(wiki.pages_needing_update(self.db_path), [])
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1", decision="賞品はしゃもじ",
                            topic="サマーカップ", source_kind="chat",
                            source_message_id=2, channel_id=7,
                            decided_on="2026-08-02", created_at="t")
        targets = wiki.pages_needing_update(self.db_path)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0][0]["topic"], "サマーカップ")
        wiki.mark_updated(self.db_path, targets[0][0]["id"], targets[0][1])
        self.assertEqual(wiki.pages_needing_update(self.db_path), [])

    def test_unrelated_decision_no_update(self):
        wiki.save_page(self.db_path, topic="サマーカップ", channel_id=7,
                       message_id=100, created_by=1)
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1", decision="定例は19時から",
                            topic="定例", source_kind="chat",
                            source_message_id=3, channel_id=8,
                            decided_on="2026-08-02", created_at="t")
        self.assertEqual(wiki.pages_needing_update(self.db_path), [])


if __name__ == "__main__":
    unittest.main()
