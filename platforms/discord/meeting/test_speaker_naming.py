#!/usr/bin/env python3
"""話者の名寄せ（起票#5）の議事録BOT側テスト。

純粋関数のみを対象にする（whisperモデル・claude CLI 不要）。
対応表そのもののテストは chatbot/test_speaker_names.py にある。
実行: ./venv/bin/python -m unittest test_speaker_naming -v
"""

import unittest

from platforms.discord.meeting.minutes_generator import build_mapping_instruction
from platforms.discord.meeting.transcriber import resolve_speaker


NAMES = {"yamada": "山田", "suzuki01": "鈴木"}
USER_MAPPING = {
    "yamada": "<@100000000000000002>",
    "suzuki01": "<@100000000000000001>",
    "unknown_user": "<@100000000000000005>",   # 辞書に無い人
}


class ResolveSpeakerTest(unittest.TestCase):
    def test_known_account_becomes_surname(self):
        self.assertEqual(resolve_speaker("yamada", NAMES), "山田")

    def test_unknown_account_kept(self):
        self.assertEqual(resolve_speaker("unknown_user", NAMES), "unknown_user")

    def test_empty_map_is_identity(self):
        """固有名詞辞書が読めない環境でも従来どおり動く（静かに眠る）。"""
        self.assertEqual(resolve_speaker("yamada", {}), "yamada")


class BuildMappingInstructionTest(unittest.TestCase):
    def test_keys_follow_surnames_after_normalization(self):
        """文字起こしが名字になった以上、対応表の見出しも名字でなければ
        LLMがTODO担当者を照合できない（名寄せ導入時の回帰点）。"""
        out = build_mapping_instruction(USER_MAPPING, NAMES)
        self.assertIn("- 山田 → <@100000000000000002>", out)
        self.assertIn("- 鈴木 → <@100000000000000001>", out)
        self.assertNotIn("yamada", out)

    def test_unmapped_account_stays_raw(self):
        out = build_mapping_instruction(USER_MAPPING, NAMES)
        self.assertIn("- unknown_user → <@100000000000000005>", out)

    def test_without_speaker_names_keeps_legacy_behavior(self):
        out = build_mapping_instruction(USER_MAPPING, {})
        self.assertIn("- yamada → <@100000000000000002>", out)

    def test_duplicate_person_collapsed(self):
        mapping = {"yamada": "<@1>", "yamada_old": "<@1>"}
        names = {"yamada": "山田", "yamada_old": "山田"}
        out = build_mapping_instruction(mapping, names)
        self.assertEqual(out.count("山田 →"), 1)

    def test_empty_mapping_returns_empty(self):
        self.assertEqual(build_mapping_instruction({}, NAMES), "")
        self.assertEqual(build_mapping_instruction(None, NAMES), "")


if __name__ == "__main__":
    unittest.main()
