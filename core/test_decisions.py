#!/usr/bin/env python3
"""決定事項台帳（decisions / 進化ロードマップ#4）のユニットテスト。

議事録からの抽出（検証つきパース）・保存・キーワード検索・
decide_reply注入用ブロックの整形を検証する。claudeは invoke_fn 注入。
"""

import os
import tempfile
import unittest

from core import db
from core import decisions


class DecisionsTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _save(self, items, **kw):
        args = {"source_kind": "minutes", "channel_id": 555,
                "source_message_id": 42, "decided_on": "2026-07-31"}
        args.update(kw)
        return decisions.save_decisions(self.db_path, "agent1", items, **args)


class ParseMinutesTest(unittest.TestCase):
    def test_valid_decisions(self):
        raw = ('{"decisions": [{"decision": "優勝賞品はしゃもじセットに決定", '
               '"topic": "夏季大会"}]}')
        out = decisions.parse_minutes_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["topic"], "夏季大会")

    def test_broken_json_or_empty(self):
        self.assertEqual(decisions.parse_minutes_response("抽出不可"), [])
        self.assertEqual(decisions.parse_minutes_response(""), [])
        self.assertEqual(
            decisions.parse_minutes_response('{"decisions": [{"topic": "x"}]}'),
            [])  # decision本文なしは捨てる

    def test_caps_and_truncates(self):
        items = [{"decision": "あ" * 500, "topic": "い" * 100}] * 40
        import json
        out = decisions.parse_minutes_response(
            json.dumps({"decisions": items}, ensure_ascii=False))
        self.assertEqual(len(out), decisions.MAX_PER_MINUTES)
        self.assertEqual(len(out[0]["decision"]), decisions.MAX_DECISION_LEN)
        self.assertEqual(len(out[0]["topic"]), decisions.MAX_TOPIC_LEN)

    def test_extract_uses_invoke_fn_with_date(self):
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"decisions": []}'

        decisions.extract_from_minutes("✅ 何かに決定", "2026-07-31",
                                       invoke_fn=fake)
        self.assertIn("2026-07-31", seen["prompt"])
        self.assertIn("✅ 何かに決定", seen["prompt"])


class SaveSearchTest(DecisionsTestBase):
    def test_save_and_keyword_search(self):
        self._save([{"decision": "Tシャツは5000〜8000円台のみ対応する方針",
                     "topic": "グッズ"},
                    {"decision": "月謝は10万円への減額を受け入れる",
                     "topic": "月謝交渉"}])
        with db.connect(self.db_path) as conn:
            hits = db.search_decisions(conn, ["月謝", "無関係語"])
            self.assertEqual(len(hits), 1)
            self.assertIn("10万円", hits[0]["decision"])
            self.assertEqual(db.count_decisions(conn), 2)

    def test_empty_keywords_return_recent(self):
        self._save([{"decision": f"決定{i}", "topic": ""} for i in range(12)])
        with db.connect(self.db_path) as conn:
            hits = db.search_decisions(conn, [], limit=5)
        self.assertEqual(len(hits), 5)
        self.assertEqual(hits[0]["decision"], "決定11")  # 新しい順

    def test_conversation_items_keep_own_source(self):
        # 会話由来はitem側のmessage_id/channel_idが優先される
        self._save([{"decision": "会場はイオンシネマで確定", "topic": "会場",
                     "message_id": 900, "channel_id": 77}],
                   source_kind="conversation", channel_id=None,
                   source_message_id=None)
        with db.connect(self.db_path) as conn:
            hits = db.search_decisions(conn, ["イオン"])
        self.assertEqual(hits[0]["source_message_id"], 900)
        self.assertEqual(hits[0]["channel_id"], 77)


class LedgerBlockTest(DecisionsTestBase):
    def test_block_contains_link_topic_and_header(self):
        self._save([{"decision": "優勝賞品はしゃもじセットに決定",
                     "topic": "夏季大会"}])
        block = decisions.build_ledger_block(self.db_path, ["しゃもじ"], "9")
        self.assertIn("決定事項台帳", block)
        self.assertIn("[夏季大会]", block)
        self.assertIn("（2026-07-31決定）", block)
        self.assertIn("discord.com/channels/9/555/42", block)

    def test_no_hits_returns_empty(self):
        self.assertEqual(
            decisions.build_ledger_block(self.db_path, ["何もない"], "9"), "")


if __name__ == "__main__":
    unittest.main()
