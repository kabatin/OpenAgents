#!/usr/bin/env python3
"""メッセージリンク/ID 参照（msgref）のユニットテスト。"""

import os
import tempfile
import unittest

from core import db
from core import msgref


class ExtractRefsTest(unittest.TestCase):
    def test_full_link(self):
        text = "これ見て https://discord.com/channels/111/222/333 どう？"
        self.assertEqual(msgref.extract_refs(text), [(222, 333)])

    def test_link_guild_filter(self):
        text = "https://discord.com/channels/111/222/333"
        # 同一guildは通る
        self.assertEqual(msgref.extract_refs(text, guild_id=111), [(222, 333)])
        # 別guildのリンクは参照しない
        self.assertEqual(msgref.extract_refs(text, guild_id=999), [])

    def test_domain_variants(self):
        for host in ("discord.com", "discordapp.com", "canary.discord.com",
                     "ptb.discord.com"):
            text = f"https://{host}/channels/1/2/3"
            self.assertEqual(msgref.extract_refs(text, guild_id=1), [(2, 3)])

    def test_raw_id(self):
        text = "このメッセージ 987654321098765432 を見て"
        self.assertEqual(msgref.extract_refs(text), [(None, 987654321098765432)])

    def test_mention_and_emoji_ids_ignored(self):
        # <@id> / <#id> / <:name:id> の中のIDは素のIDとして拾わない
        text = ("<@123456789012345678> <#223456789012345678> "
                "<:smile:323456789012345678> 見て")
        self.assertEqual(msgref.extract_refs(text), [])

    def test_dedup_link_and_raw(self):
        # リンクのmessage_idと同じ素のIDが本文にあっても1件に畳む
        text = ("https://discord.com/channels/1/2/333444555666777888 "
                "再掲: 333444555666777888")
        self.assertEqual(msgref.extract_refs(text, guild_id=1),
                         [(2, 333444555666777888)])

    def test_short_number_not_matched(self):
        self.assertEqual(msgref.extract_refs("番号 12345 と 42"), [])

    def test_max_refs_cap(self):
        ids = [str(700000000000000000 + i) for i in range(msgref.MAX_REFS + 3)]
        text = " ".join(ids)
        refs = msgref.extract_refs(text)
        self.assertEqual(len(refs), msgref.MAX_REFS)

    def test_no_refs(self):
        self.assertEqual(msgref.extract_refs("ただの雑談です"), [])
        self.assertEqual(msgref.extract_refs(""), [])
        self.assertEqual(msgref.extract_refs(None), [])


class ResolveFromDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        db.init_db(self.db_path)
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=222, name="general", type="text")
            db.upsert_user(conn, id=42, name="uriu", display_name="田中")
            db.insert_message(
                conn, id=333, channel_id=222, author_id=42,
                content="来週の締切について相談です",
                created_at="2026-07-20T09:30:00")

    def tearDown(self):
        os.unlink(self.db_path)

    def test_resolves_author_and_id(self):
        with db.connect(self.db_path) as conn:
            resolved = msgref.resolve_from_db(conn, [(222, 333)])
        self.assertIn(333, resolved)
        e = resolved[333]
        self.assertEqual(e["author"], "田中")
        self.assertEqual(e["author_id"], 42)
        self.assertEqual(e["channel"], "general")
        self.assertIn("締切", e["content"])
        self.assertFalse(e["deleted"])

    def test_raw_id_resolves_without_channel(self):
        # 素のID（channel不明）でも message_id 一意で解決できる
        with db.connect(self.db_path) as conn:
            resolved = msgref.resolve_from_db(conn, [(None, 333)])
        self.assertEqual(resolved[333]["author_id"], 42)

    def test_missing_message_absent(self):
        with db.connect(self.db_path) as conn:
            resolved = msgref.resolve_from_db(conn, [(1, 999)])
        self.assertEqual(resolved, {})

    def test_deleted_flag_preserved(self):
        with db.connect(self.db_path) as conn:
            db.mark_deleted(conn, 333)
        with db.connect(self.db_path) as conn:
            resolved = msgref.resolve_from_db(conn, [(222, 333)])
        self.assertTrue(resolved[333]["deleted"])


class BuildReferenceBlockTest(unittest.TestCase):
    def _entry(self, **over):
        base = dict(message_id=333, channel_id=222, channel="general",
                    author="田中", author_id=42, content="本文だよ",
                    created_at="2026-07-20T09:30:00")
        base.update(over)
        return msgref.make_entry(**base)

    def test_empty(self):
        self.assertEqual(msgref.build_reference_block([], guild_id=1), "")

    def test_contains_author_id_content_and_link(self):
        block = msgref.build_reference_block([self._entry()], guild_id=111)
        self.assertIn("田中", block)
        self.assertIn("ユーザーID: 42", block)   # 「誰のID？」に答えられる要
        self.assertIn("本文だよ", block)
        self.assertIn("#general", block)
        self.assertIn(
            "https://discord.com/channels/111/222/333", block)

    def test_deleted_label(self):
        block = msgref.build_reference_block(
            [self._entry(deleted=True)], guild_id=1)
        self.assertIn("削除済み", block)

    def test_content_truncated(self):
        long = "あ" * (msgref.MAX_CONTENT_LEN + 50)
        block = msgref.build_reference_block(
            [self._entry(content=long)], guild_id=1)
        self.assertIn("…", block)
        self.assertLess(len(block), len(long) + 500)

    def test_empty_content_placeholder(self):
        block = msgref.build_reference_block(
            [self._entry(content="")], guild_id=1)
        self.assertIn("本文なし", block)


if __name__ == "__main__":
    unittest.main()
