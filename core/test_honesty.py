#!/usr/bin/env python3
"""「できたフリ」検出器（honesty / 進化ロードマップ#20）のユニットテスト。

完了主張×マーカー不発の検出（成功/失敗の-#行があれば嘘ではない）と、
根拠なし断定のシャドー判定を検証する。純粋関数のみ・DB/claude不要。
"""

import unittest

from core import honesty


class RegisteredKindMixin:
    """外部連携が honesty.register() で足した検出種別を使うテストの土台。

    組み込みには無い種別（旧・社内連携が持っていたもの）を毎回登録し直し、
    テストが終わったら綺麗に戻す。登録の仕組み自体の回帰テストも兼ねる。
    """

    KIND = "sheet"

    def setUp(self):
        self._saved = (dict(honesty.CLAIMS), dict(honesty.SUCCESS_DEEDS),
                       dict(honesty.FAIL_DEEDS), dict(honesty.LABELS))
        honesty.register(
            self.KIND,
            claim=r"(?:シート|スプレッドシート)[^\n]{0,25}?"
                  r"(?:書き込|書いと|追記|更新|登録|作成|反映)"
                  r"[^\n]{0,3}?(?:しました|したっス|しときました|完了)",
            success=r"-# (?:📊 |📋 |🗑 シート)",
            fail=r"-# ⚠️ (?:シート|[^\n]{0,40}への書き込みに失敗)",
            label="シートへの書き込み")
        super().setUp()

    def tearDown(self):
        claims, success, fail, labels = self._saved
        for target, saved in ((honesty.CLAIMS, claims),
                              (honesty.SUCCESS_DEEDS, success),
                              (honesty.FAIL_DEEDS, fail),
                              (honesty.LABELS, labels)):
            target.clear()
            target.update(saved)
        honesty._rebuild()
        super().tearDown()


class RegisterTest(RegisteredKindMixin, unittest.TestCase):
    """外部連携が自分のマーカーを検出対象に足せること。"""

    def test_登録した種別が検出される(self):
        self.assertEqual(
            honesty.detect_fake_done("シートに追記しときました"), ["sheet"])

    def test_登録した種別も証拠があれば検出されない(self):
        text = ("シートに追記しときました\n"
                "-# 📊 売上台帳/8月 に1行追記（A13:D13）")
        self.assertEqual(honesty.detect_fake_done(text), [])

    def test_登録した種別はDEEDSにも反映される(self):
        self.assertIn("sheet", honesty.DEEDS)
        self.assertIn("📊", honesty.DEEDS["sheet"].pattern)

    def test_組み込み種別と区別できる(self):
        self.assertNotIn("sheet", honesty.BUILTIN_KINDS)
        self.assertIn("remind", honesty.BUILTIN_KINDS)

    def test_未登録の種別は検出されない(self):
        # 連携を入れていない環境では、シートの話をしても何も起きない
        self.tearDown()
        try:
            self.assertEqual(
                honesty.detect_fake_done("シートに追記しときました"), [])
        finally:
            self.setUp()


class FakeDoneTest(unittest.TestCase):
    def test_claim_without_deed_is_caught(self):
        for text, kind in (
                ("リマインダーを登録しました！", "remind"),
                ("リマインド設定しときました〜", "remind"),
                ("ルールとして保存したっスよ", "rule"),
                ("能力追加を起票しました", "capability")):
            self.assertEqual(honesty.detect_fake_done(text), [kind], text)

    def test_claim_with_success_note_is_clean(self):
        text = ("リマインダー登録したっス！\n"
                "-# 登録: id=3 08/01(土) 10:00 資料準備")
        self.assertEqual(honesty.detect_fake_done(text), [])

    def test_claim_with_failure_note_is_clean(self):
        # 失敗の-#行があれば実挙動は可視＝嘘ではない（二重警告しない）
        text = ("リマインダー登録しました！\n"
                "-# ⚠️ 登録できなかったっス: 日時が過去")
        self.assertEqual(honesty.detect_fake_done(text), [])
        text2 = ("ルールを登録しました\n-# ⚠️ ルール登録に失敗: scope不正")
        self.assertEqual(honesty.detect_fake_done(text2), [])

    def test_action_promise_without_deed_is_caught(self):
        # 実例2026-08-07: 実行手段が無いのに口約束して期日アラートが出続けた
        for text in ("了解っス、今日の追跡タスクはキャンセルするっス。",
                     "納期追跡から削除しときました！",
                     "そのタスクの追跡は完了にしとくっスね"):
            self.assertEqual(honesty.detect_fake_done(text), ["action"], text)

    def test_action_with_deed_or_question_is_clean(self):
        done = ("追跡タスクをキャンセルしたっス\n"
                "-# 🗑 納期追跡をキャンセル(id=1): 撮影機材の確認")
        self.assertEqual(honesty.detect_fake_done(done), [])
        fail = ("追跡タスクはキャンセルするっス\n"
                "-# ⚠️ 納期追跡: id=1 は担当の人か管理者だけが操作できるっス")
        self.assertEqual(honesty.detect_fake_done(fail), [])
        # 疑問形は主張ではない
        ask = "id=1の追跡タスクをキャンセルするっスか？"
        self.assertEqual(honesty.detect_fake_done(ask), [])

    def test_action_skip_for_agents_without_tracking(self):
        text = "追跡タスクはキャンセルするっス"
        self.assertEqual(honesty.detect_fake_done(text, skip=("action",)), [])

    def test_no_claim_is_clean(self):
        for text in ("明日の予定は3件ありますよ", "了解っス！確認しますね",
                     "リマインダー機能の使い方は〜", ""):
            self.assertEqual(honesty.detect_fake_done(text), [], text)

    def test_multiple_missing_claims(self):
        text = "リマインダーを登録しました。ルールも保存しました！"
        self.assertEqual(set(honesty.detect_fake_done(text)),
                         {"remind", "rule"})

    def test_note_names_the_missing_action(self):
        note = honesty.build_fake_done_note(["remind"])
        self.assertIn("失敗したっス", note)
        self.assertIn("リマインダー", note)

    def test_notes_are_prominent_not_subtext(self):
        # 人間は1行目を読んで判断する。訂正は本文の先頭・通常サイズで出す
        # （-# は小さいグレー文字なので読み飛ばされる）
        for note in (honesty.build_fake_done_note(["remind"]),
                     honesty.build_failed_claim_note(["sheet"])):
            self.assertFalse(note.startswith("-#"), note)
            self.assertTrue(note.startswith("⚠️ 失敗したっス"), note)

    def test_deeds_derived_from_success_and_fail(self):
        # DEEDS は成功/失敗の定義から導出＝三者がズレない
        self.assertEqual(set(honesty.DEEDS), set(honesty.CLAIMS))
        for kind, rx in honesty.SUCCESS_DEEDS.items():
            self.assertIn(rx.pattern, honesty.DEEDS[kind].pattern)
        for kind, rx in honesty.FAIL_DEEDS.items():
            self.assertIn(rx.pattern, honesty.DEEDS[kind].pattern)


class FailedClaimTest(RegisteredKindMixin, unittest.TestCase):
    def test_failure_only_with_claim_is_corrected(self):
        for text, kind in (
                ("リマインダー登録しました！\n"
                 "-# ⚠️ 登録できなかったっス: 日時が過去", "remind"),
                ("ルールを登録しました\n"
                 "-# ⚠️ ルール登録に失敗: scope不正", "rule"),
                ("シートに書き込みしときました！\n"
                 "-# ⚠️ 売上台帳 への書き込みに失敗したっス: 権限がない(403)",
                 "sheet")):
            self.assertEqual(honesty.detect_failed_claim(text), [kind], text)

    def test_success_present_is_not_corrected(self):
        text = ("シートに追記しときました\n"
                "-# 📊 売上台帳/8月 に1行追記（A13:D13）: 8/12 | 10000")
        self.assertEqual(honesty.detect_failed_claim(text), [])

    def test_no_claim_is_not_corrected(self):
        text = "-# ⚠️ お知らせの削除に失敗したっス: 権限がない(403)"
        self.assertEqual(honesty.detect_failed_claim(text), [])


class StripClaimsTest(RegisteredKindMixin, unittest.TestCase):
    """失敗したなら「できました」と言わない: 嘘の文自体を消す。"""

    KIND = "notice"

    def setUp(self):
        super().setUp()
        honesty.register(
            "notice",
            claim=r"お知らせ[^\n]{0,25}?(?:登録|作成|公開|非公開|更新|削除)"
                  r"(?:しました|したっス|しときました|完了)",
            success=r"-# (?:📰 お知らせ|🗑 お知らせ)",
            fail=r"-# ⚠️ お知らせ", label="お知らせ操作")

    def test_claim_sentence_removed_others_kept(self):
        text = ("ID:100の記事っスね。お知らせを削除しときました！\n"
                "-# ⚠️ お知らせの削除に失敗したっス: 権限がない(403)")
        out = honesty.strip_claims(text, ["notice"])
        self.assertNotIn("削除しときました", out)
        self.assertIn("ID:100の記事っスね。", out)       # 他の文は残る
        self.assertIn("-# ⚠️ お知らせの削除に失敗", out)  # 実行結果は残る

    def test_deed_lines_are_never_touched(self):
        # -# 行自体が「登録したっス」等の完了表現を含んでも消さない
        text = ("お知らせを登録しときました\n"
                "-# 📰 お知らせを下書きで登録したっス（ID: 5）「大会」")
        out = honesty.strip_claims(text, ["notice"])
        self.assertIn("-# 📰 お知らせを下書きで登録したっス", out)
        self.assertNotIn("お知らせを登録しときました\n", out)

    def test_body_can_become_empty(self):
        out = honesty.strip_claims("お知らせを削除しときました！", ["notice"])
        self.assertEqual(out, "")

    def test_other_kinds_are_left_alone(self):
        text = "リマインダーを登録しました！"
        self.assertEqual(honesty.strip_claims(text, ["notice"]), text)

    def test_empty_and_no_kinds(self):
        self.assertEqual(honesty.strip_claims("", ["notice"]), "")
        self.assertEqual(honesty.strip_claims("そのままっス", []), "そのままっス")


class UnsourcedAssertionTest(unittest.TestCase):
    def test_assertion_without_source_is_flagged(self):
        self.assertTrue(honesty.unsourced_assertion(
            "納期は8月8日で確定しています", 0))

    def test_with_link_or_hits_is_clean(self):
        text = ("納期は8月8日で確定しています "
                "https://discord.com/channels/1/2/3")
        self.assertFalse(honesty.unsourced_assertion(text, 0))
        self.assertFalse(honesty.unsourced_assertion(
            "納期は8月8日で確定しています", 5))

    def test_non_assertion_is_clean(self):
        self.assertFalse(honesty.unsourced_assertion(
            "納期はたしか8月ごろだったと思います（要確認）", 0))

    def test_excerpt_extracts_around_assertion(self):
        text = "前置き。" * 30 + "納期は8月8日で確定しています。" + "後置き。" * 30
        ex = honesty.assertion_excerpt(text)
        self.assertIn("確定", ex)
        self.assertLessEqual(len(ex), 100)


if __name__ == "__main__":
    unittest.main()
