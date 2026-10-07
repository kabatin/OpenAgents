#!/usr/bin/env python3
"""言い終わってから答える: 連投・入力中を待ってまとめて答える判定のテスト。"""

import unittest

from core import turn_wait as tw


class CompletenessTest(unittest.TestCase):
    def test_finished_requests(self):
        for t in ("会場の候補を3つ比較して表にして",
                  "明日の定例って何時からだっけ？",
                  "資料を作ってください。",
                  "この件お願いします",
                  "教えてほしい"):
            self.assertEqual(tw.completeness(t), "done", t)

    def test_unfinished_fragments(self):
        for t in ("明日の件なんだけど、",
                  "在庫の件なんですけど",
                  "あの",
                  "ちょっと相談",
                  "まず最初に",
                  "候補は以下の3つで：",
                  "えーと…",
                  ""):
            self.assertEqual(tw.completeness(t), "partial", t)

    def test_unclear(self):
        self.assertEqual(tw.completeness("先日の打ち合わせの件ありがとうございました"), "unclear")


class FireTest(unittest.TestCase):
    def _fire(self, now, **kw):
        base = dict(first_at=0, last_msg_at=0, last_typing_at=None, kind="done")
        base.update(kw)
        return tw.should_fire(now, **base)

    def test_done_fires_after_short_grace(self):
        self.assertFalse(self._fire(1))
        self.assertTrue(self._fire(2.1))

    def test_partial_waits_for_continuation(self):
        self.assertFalse(self._fire(30, kind="partial"))
        self.assertTrue(self._fire(61, kind="partial"))

    def test_typing_extends_the_wait(self):
        # 言い終わりに見えても、その人が入力中なら待つ
        self.assertFalse(self._fire(5, last_typing_at=1))
        self.assertTrue(self._fire(14, last_typing_at=1))

    def test_never_waits_beyond_the_cap(self):
        self.assertTrue(self._fire(181, kind="partial", last_msg_at=170,
                                   last_typing_at=180, max_total=180))


class MergeTest(unittest.TestCase):
    def test_joined_in_order_without_blanks(self):
        self.assertEqual(tw.merge(["明日の件なんだけど、", " ", "会場を比較して"]),
                         "明日の件なんだけど、\n会場を比較して")


class ConfigTest(unittest.TestCase):
    def test_defaults_off_and_clamped(self):
        self.assertEqual(tw.normalize(None), {"enabled": False, "max_wait_sec": 180})
        self.assertEqual(tw.normalize({"enabled": True, "max_wait_sec": 99999}),
                         {"enabled": True, "max_wait_sec": 600})
        # 誤記で黙って止まらない（数字でなければ既定）
        self.assertEqual(tw.normalize({"enabled": True, "max_wait_sec": "x"}),
                         {"enabled": True, "max_wait_sec": 180})


if __name__ == "__main__":
    unittest.main()
