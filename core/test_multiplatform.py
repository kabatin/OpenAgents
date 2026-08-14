#!/usr/bin/env python3
"""プラットフォーム跨ぎの土台の検査。

  1. core/ が特定のサービスのSDKに依存していないこと（層の分離）
  2. DBが Discord 以外のID体系も受け入れられること
  3. 既存の Discord 専用DBが、値を1つも壊さずに移行できること

実行: python -m unittest core.test_multiplatform -v
"""

import ast
import glob
import os
import sqlite3
import tempfile
import unittest

from core import chat
from core import db
from core import paths

#: core/ が import してはいけない外部SDK（プラットフォーム固有のもの）
FORBIDDEN_IN_CORE = {
    "discord", "slack_sdk", "slack_bolt", "linebot", "telegram",
    "telebot", "pytelegrambotapi",
}


class LayeringTest(unittest.TestCase):
    """core/ にプラットフォーム固有の依存が混ざっていないこと。

    ここが崩れると「Slack対応」は事実上できなくなる。人間のレビューでは
    見落とすので機械で見張る。
    """

    def _imports(self, path):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found

    def test_coreはプラットフォームSDKをimportしない(self):
        offenders = []
        for path in glob.glob(os.path.join(paths.ROOT, "core", "**", "*.py"),
                              recursive=True):
            bad = self._imports(path) & FORBIDDEN_IN_CORE
            if bad:
                offenders.append(f"{os.path.relpath(path, paths.ROOT)}: {bad}")
        self.assertEqual(
            offenders, [],
            "core/ はプラットフォーム非依存でなければなりません。"
            "該当コードは platforms/ 側へ移してください:\n" + "\n".join(offenders))

    def test_coreはplatformsをimportしない(self):
        """依存の向きは platforms → core の一方通行。"""
        offenders = []
        for path in glob.glob(os.path.join(paths.ROOT, "core", "**", "*.py"),
                              recursive=True):
            if "platforms" in self._imports(path):
                offenders.append(os.path.relpath(path, paths.ROOT))
        self.assertEqual(offenders, [], f"逆向きの依存: {offenders}")


class CapabilityTest(unittest.TestCase):
    def test_不足能力を名前で返す(self):
        class Fake:
            capabilities = frozenset({chat.CAP_MENTION})

        missing = chat.missing_capabilities(
            Fake(), [chat.CAP_MENTION, chat.CAP_VOICE])
        self.assertEqual(missing, [chat.CAP_VOICE])

    def test_不足を人間に読める文にする(self):
        text = chat.describe_missing([chat.CAP_VOICE, chat.CAP_THREAD])
        self.assertIn("音声", text)
        self.assertIn("スレッド", text)

    def test_不足なしなら空文字(self):
        self.assertEqual(chat.describe_missing([]), "")

    def test_能力名にはすべて説明がある(self):
        for cap in chat.ALL_CAPABILITIES:
            self.assertIn(cap, chat.CAPABILITY_LABELS)

    def test_capabilitiesが無いオブジェクトでも落ちない(self):
        self.assertEqual(
            chat.missing_capabilities(object(), [chat.CAP_MENTION]),
            [chat.CAP_MENTION])


class ChatTypesTest(unittest.TestCase):
    def test_display_nameは未指定ならnameを使う(self):
        u = chat.ChatUser(id="1", name="taro")
        self.assertEqual(u.display_name, "taro")

    def test_IDは文字列で持つ(self):
        # Slack の "C01234" のような非数値IDを想定した型にしてある
        c = chat.ChatChannel(id="C01234", name="general")
        self.assertIsInstance(c.id, str)

    def test_添付の種別はcontent_typeから判定する(self):
        img = chat.ChatAttachment(id="1", filename="a.png",
                                  content_type="image/png")
        vid = chat.ChatAttachment(id="2", filename="a.mp4",
                                  content_type="video/mp4")
        doc = chat.ChatAttachment(id="3", filename="a.pdf",
                                  content_type="application/pdf")
        self.assertTrue(img.is_image)
        self.assertTrue(vid.is_video)
        self.assertFalse(doc.is_image)
        self.assertFalse(doc.is_video)

    def test_メッセージはイミュータブル(self):
        m = chat.ChatMessage(id="1", channel_id="c", content="やあ",
                             created_at="2026-01-01T00:00:00+09:00",
                             author=chat.ChatUser(id="u", name="taro"))
        with self.assertRaises(Exception):
            m.content = "書き換え"


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "archive.db")

    def test_新規DBにplatform列がある(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            for table in ("channels", "users", "messages"):
                cols = [r[1] for r in conn.execute(
                    f"PRAGMA table_info({table})")]
                self.assertIn("platform", cols, table)
                self.assertIn("external_id", cols, table)

    def test_Discordの保存は今までどおり(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=111, name="general", type="text")
            row = conn.execute(
                "SELECT id, platform, external_id FROM channels").fetchone()
        # id はスノーフレークのまま、由来だけが記録される
        self.assertEqual(row, (111, "discord", "111"))

    def test_非数値IDのプラットフォームも入る(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text",
                              platform="slack", external_id="C01234")
            got = db.local_id(conn, "channels", "slack", "C01234")
        self.assertEqual(got, 1)

    def test_同じ外部IDは二重登録されない(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text",
                              platform="slack", external_id="C01234")
            with self.assertRaises(sqlite3.IntegrityError):
                db.upsert_channel(conn, id=2, name="別物", type="text",
                                  platform="slack", external_id="C01234")

    def test_プラットフォームが違えば同じ外部IDでも共存できる(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="a", type="text",
                              platform="slack", external_id="X1")
            db.upsert_channel(conn, id=2, name="b", type="text",
                              platform="telegram", external_id="X1")
            self.assertEqual(db.local_id(conn, "channels", "slack", "X1"), 1)
            self.assertEqual(db.local_id(conn, "channels", "telegram", "X1"), 2)

    def test_見つからなければNone(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            self.assertIsNone(db.local_id(conn, "users", "slack", "U999"))

    def test_external_idを持たないテーブルは拒否する(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            with self.assertRaises(ValueError):
                db.local_id(conn, "rules", "slack", "1")


class LegacyMigrationTest(unittest.TestCase):
    """Discord専用だった既存DBが、値を1つも失わずに移行できること。"""

    SNOWFLAKE = 1234567890123456789   # 19桁（丸め事故の検出用）

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "archive.db")
        # platform列が無かった頃のスキーマを手で作る
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT,
                                   type TEXT, parent_id INTEGER);
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT,
                                display_name TEXT, is_bot INTEGER DEFAULT 0);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, channel_id INTEGER,
                                   author_id INTEGER, content TEXT,
                                   created_at TEXT, edited_at TEXT,
                                   reply_to INTEGER, deleted INTEGER DEFAULT 0);
        """)
        conn.execute("INSERT INTO channels VALUES(?,?,?,?)",
                     (self.SNOWFLAKE, "general", "text", None))
        conn.execute("INSERT INTO users VALUES(?,?,?,?)",
                     (999, "taro", "太郎", 0))
        conn.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            (555, self.SNOWFLAKE, 999, "こんにちは",
             "2026-01-01T00:00:00+09:00", None, None, 0))
        conn.commit()
        conn.close()

    def test_移行してもidが1つも変わらない(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT id FROM channels").fetchone()[0],
                self.SNOWFLAKE)
            self.assertEqual(
                conn.execute("SELECT content FROM messages").fetchone()[0],
                "こんにちは")

    def test_既存行はdiscord由来として埋まる(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            row = conn.execute(
                "SELECT platform, external_id FROM channels").fetchone()
        self.assertEqual(row, ("discord", str(self.SNOWFLAKE)))

    def test_19桁IDが文字列化で丸まらない(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            ext = conn.execute(
                "SELECT external_id FROM channels").fetchone()[0]
        self.assertEqual(ext, "1234567890123456789")
        self.assertEqual(len(ext), 19)

    def test_何度実行しても壊れない(self):
        for _ in range(3):
            db.init_db(self.path)
        with db.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT platform FROM users").fetchone()[0],
                "discord")

    def test_移行後に別プラットフォームを足せる(self):
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="slack-general", type="text",
                              platform="slack", external_id="C01234")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 2)
            self.assertEqual(
                db.local_id(conn, "channels", "discord", str(self.SNOWFLAKE)),
                self.SNOWFLAKE)


if __name__ == "__main__":
    unittest.main()
