#!/usr/bin/env python3
"""未回答質問の救済（rescue / 進化ロードマップ#31）のユニットテスト。

候補抽出（時間窓・リプライ有無・重複判定・除外ch）・判定パース・
プロンプト内容・記録の重複防止を検証する。claudeは invoke_fn 注入。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core import db
from core import rescue

NOW_UTC = datetime(2026, 7, 31, 12, 0)


class RescueTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _msg(self, conn, mid, text, *, hours_ago=30, ch=1, author=1,
             is_bot=False, reply_to=None):
        db.upsert_channel(conn, id=ch, name="general", type="text")
        db.upsert_user(conn, id=author, name=f"u{author}",
                       display_name=f"ユーザー{author}", is_bot=is_bot)
        created = (NOW_UTC - timedelta(hours=hours_ago)).isoformat() \
            + "+00:00"
        db.insert_message(conn, id=mid, channel_id=ch, author_id=author,
                          content=text, created_at=created,
                          reply_to=reply_to)

    def _find(self, **kw):
        return rescue.find_candidates(self.db_path, now=NOW_UTC, **kw)


class CandidateTest(RescueTestBase):
    def test_aged_unanswered_question_is_found(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "スカジャンの納期っていつでしたっけ？",
                      hours_ago=30)
        cands = self._find()
        self.assertEqual([c["id"] for c in cands], [10])

    def test_window_bounds(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "新しい質問です？", hours_ago=2)    # 若すぎ
            self._msg(conn, 11, "古すぎる質問？", hours_ago=100)   # 古すぎ
            self._msg(conn, 12, "ちょうどいい質問？", hours_ago=30)
        self.assertEqual([c["id"] for c in self._find()], [12])

    def test_replied_question_is_excluded(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "これどうですか？", hours_ago=30)
            self._msg(conn, 11, "こうだよ", hours_ago=29, author=2,
                      reply_to=10)
        self.assertEqual(self._find(), [])

    def test_bot_and_nonquestion_and_noise_excluded(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "Botの質問？", hours_ago=30, author=9,
                      is_bot=True)
            self._msg(conn, 11, "質問じゃない発言", hours_ago=30)
            self._msg(conn, 12, "まじ？", hours_ago=30)   # 相槌ノイズ
        self.assertEqual(self._find(), [])

    def test_question_mark_inside_url_is_not_a_question(self):
        """URLのクエリ文字列の ? を質問と数えて代理回答していた（9/26・9/27）。"""
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "これ見て https://example.com/watch?v=abc&t=1",
                      hours_ago=30)
            self._msg(conn, 11, "この動画どう思います？ https://example.com/a?b=1",
                      hours_ago=30)
        self.assertEqual([c["id"] for c in self._find()], [11])

    def test_noise_does_not_block_the_queue(self):
        """弾いた候補は台帳に残らないので、古い順の枠を塞ぎ続けていた。"""
        with db.connect(self.db_path) as conn:
            for i in range(5):
                self._msg(conn, 10 + i, f"https://example.com/p?id={i}",
                          hours_ago=40)
            self._msg(conn, 50, "スカジャンの納期っていつでしたっけ？",
                      hours_ago=30)
        self.assertEqual([c["id"] for c in self._find()], [50])

    def test_excluded_channels_and_dedupe(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "除外chの質問？", hours_ago=30, ch=200)
            self._msg(conn, 11, "判定済みの質問？", hours_ago=30)
        rescue.record(self.db_path, 11, "agent1", "skipped_answered")
        self.assertEqual(self._find(exclude_channel_ids=[200]), [])


class JudgeTest(RescueTestBase):
    CAND = {"id": 10, "channel_id": 1, "channel": "general",
            "author": "人A", "author_id": 1,
            "content": "スカジャンの納期っていつでしたっけ？",
            "created_at": "2026-07-30T06:00:00+00:00"}

    def test_prompt_contains_question_and_flow(self):
        follow = [{"id": 11, "author": "山田", "is_bot": False,
                   "content": "別の話だけど…", "created_at": "t"}]
        prompt = rescue.build_judge_prompt(self.CAND, follow)
        self.assertIn("納期っていつ", prompt)
        self.assertIn("山田: 別の話だけど", prompt)
        self.assertIn("迷ったら救済しない", prompt)

    def test_prompt_shows_how_soon_each_message_came(self):
        """直後の一言「追加します」を、質問と無関係な発言として読み飛ばしていた（10/20）。"""
        follow = [{"author": "人A", "content": "追加します", "is_bot": False,
                   "created_at": "2026-07-30T06:02:10.123000+00:00"},
                  {"author": "山田", "content": "別件です", "is_bot": False,
                   "created_at": "2026-07-31T09:00:00+00:00"}]
        prompt = rescue.build_judge_prompt(self.CAND, follow)
        self.assertIn("（2分後）人A: 追加します", prompt)
        self.assertIn("（1日後）山田: 別件です", prompt)

    def test_elapsed_label(self):
        q = "2026-07-30T06:00:00+00:00"
        self.assertEqual(rescue.elapsed_label(q, "2026-07-30T06:00:20+00:00"), "直後")
        self.assertEqual(rescue.elapsed_label(q, "2026-07-30T06:59:00+00:00"), "59分後")
        self.assertEqual(rescue.elapsed_label(q, "2026-07-30T09:10:00+00:00"), "3時間後")
        self.assertEqual(rescue.elapsed_label(q, "2026-08-01T07:00:00+00:00"), "2日後")
        self.assertEqual(rescue.elapsed_label(q, "t"), "")
        self.assertEqual(rescue.elapsed_label(None, None), "")

    def test_prompt_shows_reactions_on_the_question(self):
        prompt = rescue.build_judge_prompt(
            self.CAND, [], reactions=[("✅", "山田")])
        self.assertIn("✅ 山田", prompt)

    def test_only_an_untouched_real_question_is_rescued(self):
        v = rescue.parse_judge(
            '{"is_question": true, "answered": "no", "reason": "誰も触れていない"}')
        self.assertTrue(v["rescue"])
        self.assertEqual(v["answered"], "no")

    def test_partly_answered_is_not_rescued(self):
        """一部だけ答えが出た質問に重ねて投稿していた（10/31）。"""
        v = rescue.parse_judge(
            '{"is_question": true, "answered": "partly", "reason": "日程だけ返事あり"}')
        self.assertFalse(v["rescue"])
        self.assertEqual(v["answered"], "partly")

    def test_answered_or_not_a_question_is_not_rescued(self):
        self.assertFalse(rescue.parse_judge(
            '{"is_question": true, "answered": "yes", "reason": "x"}')["rescue"])
        self.assertFalse(rescue.parse_judge(
            '{"is_question": false, "answered": "no", "reason": "メモ"}')["rescue"])

    def test_parse_judge_safe_side(self):
        for raw in ("判定できません", "", '{"is_question": true}',
                    '{"is_question": true, "answered": "たぶん"}',
                    '{"is_question": "yes", "answered": "no"}',
                    '{"rescue": true, "reason": "旧形式"}'):
            self.assertFalse(rescue.parse_judge(raw)["rescue"], raw)

    def test_judge_uses_invoke_fn(self):
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return '{"is_question": true, "answered": "no", "reason": "誰も答えていない"}'

        v = rescue.judge(self.CAND, [], model="x", invoke_fn=fake)
        self.assertTrue(v["rescue"])
        self.assertIn("納期", seen["prompt"])


class JudgeVotesTest(unittest.TestCase):
    """判定は同じ入力でも揺れる（過去17件を3回ずつ再判定して3件が1/3で反転）。
    頼まれていない投稿なので、2回とも「拾う」のときだけ拾う。"""
    CAND = JudgeTest.CAND
    YES = '{"is_question": true, "answered": "no", "reason": "未回答"}'
    NO = '{"is_question": true, "answered": "partly", "reason": "返事あり"}'

    def _run(self, answers):
        calls = []

        def fake(prompt):
            calls.append(prompt)
            return answers[len(calls) - 1]

        return rescue.judge(self.CAND, [], model="x", invoke_fn=fake), len(calls)

    def test_both_votes_needed(self):
        v, n = self._run([self.YES, self.YES])
        self.assertTrue(v["rescue"])
        self.assertEqual(n, 2)

    def test_second_vote_can_veto(self):
        v, n = self._run([self.YES, self.NO])
        self.assertFalse(v["rescue"])
        self.assertEqual(v["answered"], "partly")
        self.assertEqual(n, 2)

    def test_first_no_stops_without_a_second_call(self):
        v, n = self._run([self.NO])
        self.assertFalse(v["rescue"])
        self.assertEqual(n, 1)


class AnswerHistoryTest(unittest.TestCase):
    """回答の生成にも、判定と同じ「その後の流れ」を渡す。
    以前は質問文だけで答えていたため、済んだ直後に「今日が締め切りです」と案内した。"""

    def test_follow_ups_reach_the_answer_with_a_note(self):
        follow = [{"author": "人A", "content": "抽選終わりました", "is_bot": False}]
        h = rescue.answer_history(follow)
        self.assertEqual(h[0]["content"], rescue.FOLLOW_NOTE)
        self.assertEqual(h[1], {"author": "人A", "content": "抽選終わりました",
                                "is_bot": False})

    def test_note_reaches_the_answer_even_without_follow_ups(self):
        """その後の発言が無いと注記ごと消えて、材料なしでも投稿していた。"""
        h = rescue.answer_history([])
        self.assertEqual(len(h), 1)
        self.assertIn(rescue.NO_MATERIAL_MARK, h[0]["content"])

    def test_note_asks_for_the_marks(self):
        self.assertIn(rescue.RESOLVED_MARK, rescue.FOLLOW_NOTE)
        self.assertIn(rescue.NO_MATERIAL_MARK, rescue.FOLLOW_NOTE)

    def test_note_fits_in_one_history_message(self):
        from core import search
        self.assertNotIn("…", search.build_history(rescue.answer_history([])))

    def test_resolved_reply_detected(self):
        # 判定役が見落とした解決済みの質問を、流れを読んだ回答役の判断で止める
        self.assertTrue(rescue.is_resolved_reply("[RESOLVED]"))
        self.assertTrue(rescue.is_resolved_reply(" [RESOLVED]\n"))
        self.assertFalse(rescue.is_resolved_reply("まだ決まっていません"))
        self.assertFalse(rescue.is_resolved_reply(None))


class HoldReasonTest(unittest.TestCase):
    """頼まれていない投稿なので、出す根拠が弱いものは出さない。"""

    def test_resolved(self):
        self.assertEqual(rescue.hold_reason("[RESOLVED]"), "resolved")

    def test_no_material_mark(self):
        self.assertEqual(rescue.hold_reason(" [NO_MATERIAL]\n"), "no_material")

    def test_low_confidence_footer_alone_does_not_hold(self):
        """脚注の有無で決めない。根拠つきの回答でも、細部の1点に「要確認」の脚注が
        付くことがある（過去の質問での試走で、決定と出典を示した回答が止まった）。
        材料が無いかどうかは、回答役が中身で判断して印を出す。"""
        a = ("表示する方向で決まっています。\n参照:\n- https://discord.com/channels/1/2/3\n"
             "-# 🤔 自信度低め: もう実装済みかどうかは要確認")
        self.assertIsNone(rescue.hold_reason(a))

    def test_empty_answer(self):
        self.assertEqual(rescue.hold_reason(""), "no_material")
        self.assertEqual(rescue.hold_reason(None), "no_material")

    def test_grounded_answer_is_posted(self):
        self.assertIsNone(rescue.hold_reason(
            "納期は8/20です。\n参照:\n- https://discord.com/channels/1/2/3"))

    def test_every_hold_reason_has_a_record(self):
        for reason in ("resolved", "no_material"):
            status, action, note = rescue.HOLD_RECORD[reason]
            self.assertTrue(status.startswith("skipped_"))
            self.assertTrue(action.startswith("rescue_skip_"))
            self.assertTrue(note)


class RecordTest(RescueTestBase):
    def test_record_prevents_rejudge(self):
        with db.connect(self.db_path) as conn:
            self._msg(conn, 10, "質問です？", hours_ago=30)
        self.assertEqual(len(self._find()), 1)
        rescue.record(self.db_path, 10, "agent1", "shadow")
        self.assertEqual(self._find(), [])


if __name__ == "__main__":
    unittest.main()
