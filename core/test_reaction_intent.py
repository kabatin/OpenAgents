#!/usr/bin/env python3
"""承認・操作用のリアクションを、好き嫌いの学習から切り離す判定のテスト。"""

import unittest

from core import reaction_intent


class ApprovalPromptTest(unittest.TestCase):
    def test_operation_prompts_are_detected(self):
        for text in (
                "🌊 新しい決定…\n-# ✅=旧決定を上書き / ❌=誤検知",
                "逆算スケジュール案です\n✅で登録、❌で見送り",
                "済んでいたら👍、もう不要になっていたら❌を付けてもらえれば追うのをやめます",
                "着手していいですか？ 👍=着手 / 👎=見送り",
                "👍を押すと送信します"):
            self.assertTrue(reaction_intent.is_approval_prompt(text), text)

    def test_ordinary_answers_are_not(self):
        for text in ("了解です、登録しました👍",
                     "✅ 返信を送信しました",
                     "いい感じですね！❌マークの画像は差し替えました",
                     "", None):
            self.assertFalse(reaction_intent.is_approval_prompt(text), text)


if __name__ == "__main__":
    unittest.main()
