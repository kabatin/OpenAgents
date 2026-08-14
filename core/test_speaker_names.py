#!/usr/bin/env python3
"""話者名の名寄せ（起票#5）のユニットテスト。

固有名詞辞書の「Discord ID: アカウント名」から アカウント名→正式表記（名字）の
対応表を作る純粋関数をテストする。議事録BOT側（transcriber）もこれを使う。
実行: ./venv/bin/python -m unittest test_speaker_names -v
"""

import unittest

from core import glossary


TERMS = [
    {"term": "山田", "description": "Discord ID: yamada、別名: やまちゃん、山田さん"},
    {"term": "鈴木", "description": "Discord ID: suzuki01、別名: 鈴木さん、部長"},
    {"term": "佐藤", "description": "Discord ID: _sato_、別名: 管理者"},
    {"term": "サマーカップ", "description": "社内イベント"},  # 人物以外
    {"term": "空欄", "description": ""},
]


class SpeakerNameMapTest(unittest.TestCase):
    def test_builds_account_to_formal_name(self):
        self.assertEqual(
            glossary.speaker_name_map(TERMS),
            {"yamada": "山田", "suzuki01": "鈴木", "_sato_": "佐藤"})

    def test_ignores_terms_without_discord_id(self):
        m = glossary.speaker_name_map(TERMS)
        self.assertNotIn("サマーカップ", m.values())
        self.assertEqual(len(m), 3)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(glossary.speaker_name_map([]), {})
        self.assertEqual(glossary.speaker_name_map(None), {})

    def test_fullwidth_colon_and_spacing(self):
        terms = [{"term": "西島", "description": "Discord ID：  sayken1319 、別名: 会長"}]
        self.assertEqual(glossary.speaker_name_map(terms),
                         {"sayken1319": "西島"})

    def test_first_entry_wins_on_duplicate_account(self):
        """同じアカウントが2エントリにある場合も1つに定まる（非決定にしない）。"""
        terms = [{"term": "山田", "description": "Discord ID: yamada"},
                 {"term": "山田（旧）", "description": "Discord ID: yamada"}]
        self.assertEqual(glossary.speaker_name_map(terms),
                         {"yamada": "山田"})


class ResolveSpeakerTest(unittest.TestCase):
    MAP = {"yamada": "山田", "suzuki01": "鈴木"}

    def test_known_account_becomes_formal_name(self):
        self.assertEqual(
            glossary.resolve_speaker("yamada", self.MAP), "山田")

    def test_unknown_account_falls_back_to_raw(self):
        """辞書に無い人はアカウント名のまま（発言を消さない安全側）。"""
        self.assertEqual(
            glossary.resolve_speaker("unknown_user", self.MAP), "unknown_user")

    def test_empty_map_is_identity(self):
        self.assertEqual(glossary.resolve_speaker("suzuki01", {}), "suzuki01")

    def test_none_map_is_identity(self):
        self.assertEqual(glossary.resolve_speaker("suzuki01", None), "suzuki01")

    def test_blank_speaker_unchanged(self):
        self.assertEqual(glossary.resolve_speaker("", self.MAP), "")


class ResolveParticipantsTest(unittest.TestCase):
    MAP = {"yamada": "山田", "suzuki01": "鈴木", "_sato_": "佐藤"}

    def test_maps_and_preserves_order(self):
        self.assertEqual(
            glossary.resolve_participants(
                ["suzuki01", "unknown_user", "yamada"], self.MAP),
            ["鈴木", "unknown_user", "山田"])

    def test_dedupes_same_person(self):
        self.assertEqual(
            glossary.resolve_participants(["yamada", "yamada"], self.MAP),
            ["山田"])

    def test_empty(self):
        self.assertEqual(glossary.resolve_participants([], self.MAP), [])
        self.assertEqual(glossary.resolve_participants(None, self.MAP), [])


if __name__ == "__main__":
    unittest.main()
