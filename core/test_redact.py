#!/usr/bin/env python3
"""墨消し（機密を含む投稿の本文をDBから消す）のテスト。

背景: 共有メールのパスワードが平文で投稿され、
archive.db に本文ごと残っていた。論理削除（mark_deleted）は本文を残すうえ、
バックフィルと編集イベントが本文を書き戻すので、消したつもりで復活する。

実行: ./venv/bin/python -m unittest core.test_redact
"""

import os
import tempfile
import unittest

from core import db
from core import search

SECRET = "Hunter2-Zebra#Quartz"


class RedactTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text")
            db.upsert_user(conn, id=10, name="k", display_name="管理者")
            db.insert_message(conn, id=100, channel_id=1, author_id=10,
                              content=f"共有メールのPWは {SECRET} です",
                              created_at="2026-10-21T10:00")
            db.insert_message(conn, id=101, channel_id=1, author_id=10,
                              content="共有メールの件は了解です",
                              created_at="2026-10-21T10:01")

    def tearDown(self):
        self.tmp.cleanup()

    def _content(self, mid=100):
        with db.connect(self.path) as conn:
            return conn.execute("SELECT content, deleted FROM messages WHERE id=?",
                                (mid,)).fetchone()

    def _fts_hits(self, term):
        with db.connect(self.path) as conn:
            return [r[0] for r in conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
                ('"' + term + '"',))]

    def _redact(self, mid=100):
        with db.connect(self.path) as conn:
            return db.redact_message(conn, mid)

    def test_content_is_gone_and_hidden_from_search(self):
        self.assertEqual(self._fts_hits("Zebra"), [100])     # 前提: 消す前は引ける
        self.assertTrue(self._redact())
        content, deleted = self._content()
        self.assertNotIn(SECRET, content)
        self.assertEqual(content, db.REDACTED_TEXT)
        self.assertEqual(deleted, db.DELETED_REDACTED)
        self.assertEqual(self._fts_hits("Zebra"), [])
        ids = [r["id"] for r in search.search_messages(self.path, ["共有メール"])]
        self.assertEqual(ids, [101])                         # 隣の投稿は残る

    def test_backfill_does_not_bring_it_back(self):
        """Discordに投稿が残っていれば、再取得で同じIDが insert_message に来る。"""
        self._redact()
        with db.connect(self.path) as conn:
            db.insert_message(conn, id=100, channel_id=1, author_id=10,
                              content=f"共有メールのPWは {SECRET} です",
                              created_at="2026-10-21T10:00")
        self.assertEqual(self._content(), (db.REDACTED_TEXT, db.DELETED_REDACTED))
        self.assertEqual(self._fts_hits("Zebra"), [])

    def test_edit_does_not_bring_it_back(self):
        self._redact()
        with db.connect(self.path) as conn:
            db.update_content(conn, id=100, content=f"訂正: {SECRET}",
                              edited_at="2026-10-21T11:00")
        self.assertEqual(self._content(), (db.REDACTED_TEXT, db.DELETED_REDACTED))
        self.assertEqual(self._fts_hits("Zebra"), [])

    def test_discord_delete_after_redact_keeps_index_sound(self):
        """墨消しの後でDiscord側も削除される（頼んでいる手順）。索引を壊さない。"""
        self._redact()
        with db.connect(self.path) as conn:
            db.mark_deleted(conn, 100)
            db.mark_deleted(conn, 100)
        self.assertEqual(self._content(), (db.REDACTED_TEXT, db.DELETED_REDACTED))
        with db.connect(self.path) as conn:
            conn.execute("INSERT INTO messages_fts(messages_fts) "
                         "VALUES('integrity-check')")
        self.assertEqual(self._fts_hits("了解です"), [101])

    def test_already_deleted_message_can_be_redacted(self):
        """論理削除は本文を残す。削除済みの投稿も墨消しできること。"""
        with db.connect(self.path) as conn:
            db.mark_deleted(conn, 100)
        self.assertIn(SECRET, self._content()[0])            # 前提: 残っている
        self.assertTrue(self._redact())
        self.assertEqual(self._content(), (db.REDACTED_TEXT, db.DELETED_REDACTED))
        with db.connect(self.path) as conn:
            conn.execute("INSERT INTO messages_fts(messages_fts) "
                         "VALUES('integrity-check')")

    def test_reference_shows_placeholder_as_deleted(self):
        self._redact()
        with db.connect(self.path) as conn:
            got = db.get_message(conn, 100)
        self.assertTrue(got["deleted"])
        self.assertEqual(got["content"], db.REDACTED_TEXT)

    def test_unknown_id_and_repeat(self):
        self.assertFalse(self._redact(999))
        self.assertTrue(self._redact())
        self.assertFalse(self._redact())                     # 2回目は何もしない


class ScrubTest(unittest.TestCase):
    """DBファイルを直接読んでも取り出せないこと（行を書き換えるだけでは残る）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text")
            db.upsert_user(conn, id=10, name="k", display_name="管理者")
            for i in range(200):       # 索引に区画が複数できる程度の量
                db.insert_message(conn, id=1000 + i, channel_id=1, author_id=10,
                                  content=f"ふつうの会話 その{i} keepme-{i:04d}",
                                  created_at="2026-10-21T09:00")
        with db.connect(self.path) as conn:
            db.insert_message(conn, id=100, channel_id=1, author_id=10,
                              content=f"PWは {SECRET} です\nhttps://example.test/login",
                              created_at="2026-10-21T10:00")

    def tearDown(self):
        self.tmp.cleanup()

    def _raw(self):
        blob = b""
        for p in (self.path, self.path + "-wal"):
            if os.path.exists(p):
                with open(p, "rb") as f:
                    blob += f.read()
        return blob

    def _has(self, text):
        # assertIn だと失敗時にDBファイル丸ごとが出力される
        return text.encode() in self._raw()

    def test_bytes_are_gone_from_the_files(self):
        from core import redact_message
        self.assertTrue(self._has(SECRET))          # 前提: 消す前はある
        res = redact_message.redact(self.path, [100, 999])
        self.assertEqual(res["redacted"], [100])
        self.assertEqual((res["unique"], res["shared"]), (2, 0))
        self.assertEqual(res["leftovers"], 0)
        self.assertFalse(self._has(SECRET))
        self.assertTrue(self._has("keepme-0199"))           # 他の投稿は無傷

    def test_strings_shared_with_other_posts_are_reported_not_flagged(self):
        """メールアドレス等は他の投稿にもある。残っていて正しいので警告にしない。
        ただし数は返す（機密そのものが他の投稿にもある場合に気づけるように）。"""
        from core import redact_message
        with db.connect(self.path) as conn:
            db.insert_message(conn, id=102, channel_id=1, author_id=10,
                              content="ログインは https://example.test/login から",
                              created_at="2026-10-21T10:05")
        res = redact_message.redact(self.path, [100])
        self.assertEqual((res["unique"], res["shared"], res["leftovers"]), (1, 1, 0))
        self.assertFalse(self._has(SECRET))
        self.assertTrue(self._has("https://example.test/login"))

    def test_describe_never_returns_content(self):
        from core import redact_message
        got = redact_message.describe(self.path, [100, 999])
        self.assertNotIn(SECRET, repr(got))
        self.assertEqual(got[0]["state"], "公開中")
        self.assertFalse(got[1]["found"])

    def test_cli_is_dry_run_without_yes(self):
        import contextlib
        import io
        from core import redact_message
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(redact_message.main(["100", "--db", self.path]), 0)
        self.assertNotIn(SECRET, buf.getvalue())
        self.assertTrue(self._has(SECRET))          # 何も消していない
        with contextlib.redirect_stdout(buf):
            self.assertEqual(
                redact_message.main(["100", "--db", self.path, "--yes"]), 0)
        self.assertFalse(self._has(SECRET))


if __name__ == "__main__":
    unittest.main()
