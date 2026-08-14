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

    def test_approve_supersedes_only_decisions(self):
        old = self._decision("旧決定")
        new_id = self._decision("新決定")
        pid = ripple.register(self.db_path, new_id,
                              [{"kind": "decision", "id": old, "why": "w"}])
        ripple.set_message(self.db_path, pid, 500)
        self.assertEqual(ripple.approve(self.db_path, 500), 1)
        with db.connect(self.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM decisions WHERE id=?",
                (old,)).fetchone()[0]
        self.assertEqual(status, "superseded")
        self.assertIsNone(ripple.approve(self.db_path, 500))   # CAS

    def test_dismiss(self):
        new_id = self._decision("新決定")
        pid = ripple.register(self.db_path, new_id, [])
        ripple.set_message(self.db_path, pid, 501)
        self.assertTrue(ripple.dismiss(self.db_path, 501))
        self.assertFalse(ripple.dismiss(self.db_path, 501))

    def test_proposal_text(self):
        text = ripple.build_proposal(
            {"decision": "9/5に延期"},
            [{"kind": "decision", "id": 3, "why": "開催日が矛盾"},
             {"kind": "reminder", "id": 14, "why": "8/29前提の告知"}])
        self.assertIn("旧決定 id=3", text)
        self.assertIn("リマインダー id=14", text)
        self.assertIn("✅", text)


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
            "作るっス！\n[WIKI: サマーカップ]\n[WIKI: サマーカップ]")
        self.assertEqual(text, "作るっス！")
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
