#!/usr/bin/env python3
"""育つ土台（rules）のユニットテスト。"""

from datetime import datetime
import os
import tempfile
import unittest

from core import db
from core import rules


class RulesParseTest(unittest.TestCase):
    def test_valid_scopes(self):
        for scope in ("global", "channel", "user"):
            r = rules.parse_rule(f"{scope} | 何か守る")
            self.assertEqual(r["scope"], scope)
            self.assertEqual(r["text"], "何か守る")

    def test_missing_scope_separator(self):
        with self.assertRaises(ValueError):
            rules.parse_rule("global ルール本文")  # | が無い

    def test_bad_scope(self):
        with self.assertRaises(ValueError):
            rules.parse_rule("everyone | だめ")

    def test_empty_text(self):
        with self.assertRaises(ValueError):
            rules.parse_rule("global |   ")

    def test_too_long(self):
        with self.assertRaises(ValueError):
            rules.parse_rule("global | " + "あ" * (rules.MAX_RULE_LEN + 1))


class RulesExtractMarkersTest(unittest.TestCase):
    def test_extracts_and_strips_all(self):
        ans = ("了解っス、以降そうするっスね。\n"
               "[RULE: channel | 緊急と書かれたらメンション]\n"
               "[RULE_CANCEL: 5]\n"
               "[CAPABILITY: 動画の書き出し機能]")
        text, adds, cancels, caps, errs = rules.extract_markers(ans)
        self.assertEqual(text, "了解っス、以降そうするっスね。")
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["scope"], "channel")
        self.assertEqual(cancels, [5])
        self.assertEqual(caps, ["動画の書き出し機能"])
        self.assertEqual(errs, [])

    def test_no_markers(self):
        text, adds, cancels, caps, errs = rules.extract_markers("普通の返信")
        self.assertEqual(text, "普通の返信")
        self.assertEqual((adds, cancels, caps, errs), ([], [], [], []))

    def test_bad_rule_recorded_as_error_but_stripped(self):
        text, adds, cancels, caps, errs = rules.extract_markers(
            "はい[RULE: なんか変]")
        self.assertEqual(text, "はい")     # マーカーは必ず除去
        self.assertEqual(adds, [])
        self.assertEqual(len(errs), 1)


class RulesScopeTest(unittest.TestCase):
    def test_scope_key(self):
        self.assertEqual(rules.scope_key("global", channel_id=1, user_id=2),
                         "global")
        self.assertEqual(rules.scope_key("channel", channel_id=1, user_id=2),
                         "channel:1")
        self.assertEqual(rules.scope_key("user", channel_id=1, user_id=2),
                         "user:2")

    def test_context_scopes(self):
        self.assertEqual(rules.context_scopes(10, 20),
                         ["global", "channel:10", "user:20"])

    def test_build_rules_block_empty(self):
        self.assertEqual(rules.build_rules_block([]), "")

    def test_build_rules_block(self):
        block = rules.build_rules_block([
            {"scope": "global", "rule_text": "税込表記"},
            {"scope": "channel:5", "rule_text": "緊急でメンション"}])
        self.assertIn("税込表記", block)
        self.assertIn("このチャンネル", block)


class RulesDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_add_get_scope_and_agent_isolation(self):
        with db.connect(self.db_path) as conn:
            db.add_rule(conn, agent_id="agent1", scope="global",
                        rule_text="A", created_by="u1", source_msg_id=1,
                        created_at="t")
            db.add_rule(conn, agent_id="agent1", scope="channel:9",
                        rule_text="B", created_by="u1", source_msg_id=2,
                        created_at="t")
            db.add_rule(conn, agent_id="agent1", scope="user:7",
                        rule_text="C", created_by="u1", source_msg_id=3,
                        created_at="t")
            db.add_rule(conn, agent_id="agent2", scope="global",
                        rule_text="D", created_by="u1", source_msg_id=4,
                        created_at="t")
        with db.connect(self.db_path) as conn:
            got = db.get_active_rules(
                conn, "agent1", rules.context_scopes(9, 7))
        texts = [r["rule_text"] for r in got]
        self.assertEqual(texts, ["A", "B", "C"])  # agent1の該当scopeのみ、Dは別agent

    def test_scope_filter_excludes_other_channel(self):
        with db.connect(self.db_path) as conn:
            db.add_rule(conn, agent_id="agent1", scope="channel:9",
                        rule_text="B", created_by="u1", source_msg_id=1,
                        created_at="t")
            got = db.get_active_rules(
                conn, "agent1", rules.context_scopes(999, 7))  # 別ch
        self.assertEqual(got, [])

    def test_deactivate(self):
        with db.connect(self.db_path) as conn:
            rid = db.add_rule(conn, agent_id="agent1", scope="global",
                              rule_text="X", created_by="u1", source_msg_id=1,
                              created_at="t")
        with db.connect(self.db_path) as conn:
            removed = db.deactivate_rule(conn, rid, "agent1")
            self.assertEqual(removed, "X")
        with db.connect(self.db_path) as conn:
            self.assertEqual(
                db.get_active_rules(conn, "agent1", ["global"]), [])
            # 他agentが同idを消せない
            self.assertIsNone(db.deactivate_rule(conn, rid, "agent2"))

    def test_get_rule_for_ownership(self):
        with db.connect(self.db_path) as conn:
            rid = db.add_rule(conn, agent_id="agent1", scope="user:5",
                              rule_text="Y", created_by="u9", source_msg_id=1,
                              created_at="t")
            r = db.get_rule(conn, rid, "agent1")
            self.assertEqual(r["created_by"], "u9")
            self.assertEqual(r["scope"], "user:5")
            # 別agentからは見えない
            self.assertIsNone(db.get_rule(conn, rid, "agent2"))
            # 無効化後は None
            db.deactivate_rule(conn, rid, "agent1")
            self.assertIsNone(db.get_rule(conn, rid, "agent1"))

    def test_capability_request(self):
        with db.connect(self.db_path) as conn:
            cid = db.add_capability_request(
                conn, agent_id="agent3", description="動画編集",
                context="依頼文", requested_by="u1", source_msg_id=1,
                created_at="t")
            self.assertTrue(cid)
            n = conn.execute(
                "SELECT COUNT(*) FROM capability_requests "
                "WHERE status='open'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_feedback_dedup_and_remove(self):
        with db.connect(self.db_path) as conn:
            for _ in range(3):  # 同一(msg,user,value)は1件に集約
                db.add_feedback(conn, message_id=100, agent_id="agent1",
                                kind="reaction", value="up", user_id="u1",
                                created_at="t")
            n = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            self.assertEqual(n, 1)
            db.remove_feedback(conn, message_id=100, user_id="u1", value="up")
            n = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        self.assertEqual(n, 0)

    def test_message_author(self):
        with db.connect(self.db_path) as conn:
            db.upsert_user(conn, id=42, name="a", display_name="A")
            db.insert_message(conn, id=7, channel_id=1, author_id=42,
                              content="hi", created_at="t")
            self.assertEqual(db.message_author(conn, 7), 42)
            self.assertIsNone(db.message_author(conn, 999))


class RulesDurationTest(unittest.TestCase):
    def test_parse_duration_units(self):
        from datetime import timedelta
        self.assertEqual(rules.parse_duration("7d"), timedelta(days=7))
        self.assertEqual(rules.parse_duration("24h"), timedelta(hours=24))
        self.assertEqual(rules.parse_duration("2w"), timedelta(weeks=2))
        self.assertEqual(rules.parse_duration("1m"), timedelta(days=30))

    def test_parse_duration_invalid(self):
        self.assertIsNone(rules.parse_duration("しばらく"))
        self.assertIsNone(rules.parse_duration("0d"))       # 0以下は無効
        self.assertIsNone(rules.parse_duration("999d"))     # 上限超
        self.assertIsNone(rules.parse_duration(""))
        self.assertIsNone(rules.parse_duration(None))

    def test_parse_rule_with_duration(self):
        r = rules.parse_rule("user | 7d | しばらく敬語で")
        self.assertEqual(r["scope"], "user")
        self.assertEqual(r["duration"], "7d")
        self.assertEqual(r["text"], "しばらく敬語で")

    def test_parse_rule_without_duration_is_permanent(self):
        r = rules.parse_rule("global | 税込で表記")
        self.assertIsNone(r["duration"])
        self.assertEqual(r["text"], "税込で表記")

    def test_second_field_not_duration_is_text(self):
        # 期限トークンでない2つ目フィールドは本文の一部（誤って期限扱いしない）
        r = rules.parse_rule("user | 10時に必ず返信する")
        self.assertIsNone(r["duration"])
        self.assertEqual(r["text"], "10時に必ず返信する")

    def test_expiry_from(self):
        now = datetime(2026, 7, 17, 12, 0)
        self.assertEqual(rules.expiry_from("7d", now), "2026-07-24T12:00")
        self.assertIsNone(rules.expiry_from(None, now))
        self.assertIsNone(rules.expiry_from("しばらく", now))


class RulesExpiryDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_expired_rule_excluded(self):
        with db.connect(self.db_path) as conn:
            db.add_rule(conn, agent_id="agent1", scope="user:5",
                        rule_text="期限切れ", created_by="5", source_msg_id=1,
                        created_at="2026-07-01T00:00",
                        expires_at="2026-07-10T00:00")
            db.add_rule(conn, agent_id="agent1", scope="user:5",
                        rule_text="まだ有効", created_by="5", source_msg_id=2,
                        created_at="2026-07-01T00:00",
                        expires_at="2026-07-31T00:00")
            db.add_rule(conn, agent_id="agent1", scope="user:5",
                        rule_text="恒久", created_by="5", source_msg_id=3,
                        created_at="2026-07-01T00:00", expires_at=None)
        with db.connect(self.db_path) as conn:
            got = db.get_active_rules(conn, "agent1", ["user:5"],
                                      now="2026-07-17T12:00")
        texts = [r["rule_text"] for r in got]
        self.assertEqual(texts, ["まだ有効", "恒久"])  # 期限切れは除外

    def test_no_now_returns_all(self):
        with db.connect(self.db_path) as conn:
            db.add_rule(conn, agent_id="agent1", scope="user:5",
                        rule_text="A", created_by="5", source_msg_id=1,
                        created_at="t", expires_at="2000-01-01T00:00")
            got = db.get_active_rules(conn, "agent1", ["user:5"])  # now未指定
        self.assertEqual(len(got), 1)  # 期限フィルタなし

    def test_migration_adds_column(self):
        # expires_atが無い旧スキーマのrulesにも init_db が列を足す（冪等）
        import sqlite3
        p = self.db_path + ".old"
        c = sqlite3.connect(p)
        c.executescript(
            "CREATE TABLE rules(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "agent_id TEXT, scope TEXT, rule_text TEXT, created_by TEXT,"
            "source_msg_id INTEGER, active INTEGER DEFAULT 1, created_at TEXT)")
        c.execute("INSERT INTO rules(agent_id,scope,rule_text,created_by,"
                  "source_msg_id,active,created_at) "
                  "VALUES('agent1','global','旧ルール','u',1,1,'t')")
        c.commit(); c.close()
        db.init_db(p)  # マイグレーション
        with db.connect(p) as conn:
            got = db.get_active_rules(conn, "agent1", ["global"],
                                      now="2026-07-17T12:00")
        os.unlink(p)
        self.assertEqual(len(got), 1)          # 旧ルールは恒久扱いで生存
        self.assertIsNone(got[0]["expires_at"])


class RulesEndToEndInjectionTest(unittest.TestCase):
    """保存したルールが次回のContextに載る（コード変更ゼロで効く）ことの確認。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_saved_rule_appears_in_next_context(self):
        # 1) 「channelスコープ」でルール保存
        scope = rules.scope_key("channel", channel_id=55, user_id=88)
        with db.connect(self.db_path) as conn:
            db.add_rule(conn, agent_id="agent1", scope=scope,
                        rule_text="「緊急」で管理者にメンション",
                        created_by="88", source_msg_id=1, created_at="t")
        # 2) 次イベントで同じch/userのContextを組むと注入される
        with db.connect(self.db_path) as conn:
            active = db.get_active_rules(
                conn, "agent1", rules.context_scopes(55, 88))
        block = rules.build_rules_block(active)
        self.assertIn("緊急", block)
        # 別チャンネルには載らない（scope分離）
        with db.connect(self.db_path) as conn:
            other = db.get_active_rules(
                conn, "agent1", rules.context_scopes(999, 88))
        self.assertEqual(rules.build_rules_block(other), "")


class CorrectionTest(unittest.TestCase):
    """自己訂正の学習化（RM#17）: 一次フィルタと学習指示。"""

    def test_detects_correction_phrases(self):
        for text in ("それ違うよ、正しくは19時開始",
                     "19:30じゃなくて19:00だよ",
                     "その金額は間違ってる",
                     "訂正です: 会場はB館"):
            self.assertTrue(rules.looks_like_correction(text), text)

    def test_ignores_normal_speech(self):
        for text in ("ありがとう！", "明日の予定を教えて",
                     "了解です、進めましょう", ""):
            self.assertFalse(rules.looks_like_correction(text), text)

    def test_note_teaches_rule_marker_with_prefix(self):
        note = rules.build_correction_note()
        self.assertIn("[RULE:", note)
        self.assertIn("訂正: ", note)
        self.assertIn("勝手に保存しない", note)
