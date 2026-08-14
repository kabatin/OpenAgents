#!/usr/bin/env python3
"""メンション→起票ルーティングの正規化テスト（安全弁の検証が主目的）。"""

import unittest

from platforms.discord.dev import router


class ParseRouteTest(unittest.TestCase):
    OPEN = {2, 3, 4}

    def test_start_valid_id(self):
        d = router.parse_route(
            '{"action":"start","req_id":4,"reply":"#4着手するっす！"}', self.OPEN)
        self.assertEqual(d["action"], "start")
        self.assertEqual(d["req_id"], 4)

    def test_start_unknown_id_downgraded_to_chat(self):
        # 実在しない番号での start は握りつぶす（誤実装防止の安全弁）
        d = router.parse_route(
            '{"action":"start","req_id":99,"reply":"x"}', self.OPEN)
        self.assertEqual(d["action"], "chat")
        self.assertIsNone(d["req_id"])

    def test_start_null_id_downgraded(self):
        d = router.parse_route(
            '{"action":"start","req_id":null,"reply":"x"}', self.OPEN)
        self.assertEqual(d["action"], "chat")

    def test_ask(self):
        d = router.parse_route(
            '{"action":"ask","req_id":null,"reply":"どれっすか？"}', self.OPEN)
        self.assertEqual(d["action"], "ask")
        self.assertIn("どれ", d["reply"])

    def test_chat(self):
        d = router.parse_route(
            '{"action":"chat","req_id":null,"reply":"元気っす"}', self.OPEN)
        self.assertEqual(d["action"], "chat")
        self.assertEqual(d["reply"], "元気っす")

    def test_json_embedded_in_prose(self):
        d = router.parse_route(
            'はい！\n{"action":"start","req_id":2,"reply":"ok"}\nどうぞ', self.OPEN)
        self.assertEqual(d["action"], "start")
        self.assertEqual(d["req_id"], 2)

    def test_garbage_and_empty_are_chat(self):
        self.assertEqual(router.parse_route("なんも", self.OPEN)["action"], "chat")
        self.assertEqual(router.parse_route("", self.OPEN)["action"], "chat")

    def test_malformed_json_is_chat(self):
        d = router.parse_route('{"action":"start", req_id:4}', self.OPEN)
        self.assertEqual(d["action"], "chat")


class BuildSystemTest(unittest.TestCase):
    def test_includes_caps_and_schema(self):
        s = router.build_route_system(
            "PERSONA", "STATUS", [{"id": 4, "desc": "リマインダー改修"}])
        self.assertIn("#4", s)
        self.assertIn("リマインダー改修", s)
        self.assertIn("action", s)
        self.assertIn("PERSONA", s)

    def test_empty_caps(self):
        s = router.build_route_system("P", "S", [])
        self.assertIn("起票なし", s)


if __name__ == "__main__":
    unittest.main()
