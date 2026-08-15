#!/usr/bin/env python3
"""bot.py / agent_runtime の純粋ヘルパーのユニットテスト。"""

import os
import unittest

from platforms.discord import agent_runtime
from platforms.discord import bot


class TrailingBotCountTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bot.trailing_bot_count([]), 0)

    def test_human_tail_resets(self):
        h = [{"is_bot": True}, {"is_bot": False}]
        self.assertEqual(bot.trailing_bot_count(h), 0)

    def test_counts_trailing_bots_only(self):
        h = [{"is_bot": True}, {"is_bot": False},
             {"is_bot": True}, {"is_bot": True}]
        self.assertEqual(bot.trailing_bot_count(h), 2)

    def test_all_bots(self):
        self.assertEqual(bot.trailing_bot_count([{"is_bot": True}] * 5), 5)


class ExtractImageMarkerTest(unittest.TestCase):
    def test_no_marker(self):
        text, prompt, caption = bot.extract_image_marker("普通の回答です")
        self.assertEqual(text, "普通の回答です")
        self.assertIsNone(prompt)
        self.assertIsNone(caption)

    def test_marker_at_end(self):
        text, prompt, caption = bot.extract_image_marker(
            "ロゴ案作りますね！\n[IMAGE: team logo, phoenix]")
        self.assertEqual(text, "ロゴ案作りますね！")
        self.assertEqual(prompt, "team logo, phoenix")
        self.assertIsNone(caption)

    def test_marker_with_caption(self):
        text, prompt, caption = bot.extract_image_marker(
            "ロゴ案いきますねぇ！\n[IMAGE: team logo, phoenix]\n"
            "[CAPTION: できましたっ！余白広めで仕上げてます🎨]")
        self.assertEqual(text, "ロゴ案いきますねぇ！")
        self.assertEqual(prompt, "team logo, phoenix")
        self.assertEqual(caption, "できましたっ！余白広めで仕上げてます🎨")

    def test_caption_without_image_ignored(self):
        text, prompt, caption = bot.extract_image_marker(
            "本文だけ [CAPTION: これは無視されない?]")
        self.assertIsNone(prompt)
        self.assertIsNone(caption)

    def test_marker_only(self):
        text, prompt, caption = bot.extract_image_marker("[IMAGE: cat]")
        self.assertEqual(text, "")
        self.assertEqual(prompt, "cat")


class AgentWiringTest(unittest.TestCase):
    """設定の検査そのものは core/test_config.py が持つ。
    ここでは Discord層が設定を正しく受け取れているかだけを見る。"""

    def test_設定の検査はcoreに委譲されている(self):
        from core import config as core_config
        self.assertIs(agent_runtime.validate_config, core_config.validate)
        self.assertIs(agent_runtime.load_config, core_config.load)

    def test_持っているエージェントのpersona_filesは実在する(self):
        for a in bot.AGENTS:
            for path in a.get("persona_files") or []:
                self.assertTrue(os.path.exists(bot._resolve(path)),
                                f"{a['id']}: {path} が存在しない")


class ContentGateTest(unittest.TestCase):
    def test_text_always_passes(self):
        for trig in ("home", "human_mention", "agent_mention"):
            self.assertTrue(bot._content_gate(trig, "こんにちは", False))
            self.assertTrue(bot._content_gate(trig, "こんにちは", True))

    def test_attachment_only_needs_mention(self):
        self.assertTrue(bot._content_gate("human_mention", "", True))
        self.assertTrue(bot._content_gate("agent_mention", " ", True))
        self.assertFalse(bot._content_gate("home", "", True))

    def test_empty_everything_blocked(self):
        for trig in ("home", "human_mention", "agent_mention"):
            self.assertFalse(bot._content_gate(trig, "", False))


class CollectRefImagePathsTest(unittest.TestCase):
    @staticmethod
    def _saved(kind, path):
        return {"path": path, "name": os.path.basename(path),
                "kind": kind, "orig": os.path.basename(path)}

    def test_images_only(self):
        saved = [self._saved("image", "/t/01-a.png"),
                 self._saved("pdf", "/t/02-b.pdf"),
                 self._saved("image", "/t/03-c.jpg"),
                 self._saved("text", "/t/04-d.md")]
        self.assertEqual(bot.collect_ref_image_paths(saved),
                         ["/t/01-a.png", "/t/03-c.jpg"])

    def test_cap_at_max(self):
        saved = [self._saved("image", f"/t/{i:02d}.png") for i in range(6)]
        self.assertEqual(len(bot.collect_ref_image_paths(saved)),
                         agent_runtime.MAX_REF_IMAGES)
        # 先頭優先（本文の添付がリプライ先より先に来る並びを維持）
        self.assertEqual(bot.collect_ref_image_paths(saved)[0], "/t/00.png")

    def test_empty(self):
        self.assertEqual(bot.collect_ref_image_paths([]), [])


class ImageSkillNoteTest(unittest.TestCase):
    def test_mentions_reference_images(self):
        # 参考画像の受け取り方（添付/リプライ）と範囲外時の誘導が
        # スキル指示に含まれること
        self.assertIn("参考画像", bot.IMAGE_SKILL_NOTE)
        self.assertIn("リプライ", bot.IMAGE_SKILL_NOTE)
        self.assertIn("[IMAGE:", bot.IMAGE_SKILL_NOTE)


class ShouldWebhookRespondTest(unittest.TestCase):
    """ループ安全性の要（純粋関数のゲート判定）。"""

    def _base(self, **over):
        kw = dict(has_webhook_agents=True, home_agent_id="keiri",
                  webhook_id=None, author_is_bot=False, author_id=100,
                  self_user_id=999, mention_ids=[], registered_ids={999, 998},
                  text="質問っス")
        kw.update(over)
        return agent_runtime.should_webhook_respond(**kw)

    def test_happy_path(self):
        self.assertTrue(self._base())

    def test_no_personas(self):
        self.assertFalse(self._base(has_webhook_agents=False))

    def test_not_home_channel(self):
        self.assertFalse(self._base(home_agent_id=None))

    def test_webhook_message_blocked(self):  # 自人格の投稿→無限ループ遮断
        self.assertFalse(self._base(webhook_id=555))

    def test_bot_author_blocked(self):
        self.assertFalse(self._base(author_is_bot=True))

    def test_self_author_blocked(self):
        self.assertFalse(self._base(author_id=999, self_user_id=999))

    def test_other_agent_mentioned_yields(self):  # 本物Bot名指し→譲る
        self.assertFalse(self._base(mention_ids=[998]))

    def test_empty_text_blocked(self):
        self.assertFalse(self._base(text="   "))


class ContextHistoryWindowTest(unittest.TestCase):
    """「直近の会話」の参照件数（能力起票#8: 会話の連続性を追えるよう増やす）。"""

    def test_wider_than_loop_guard_window(self):
        # ループガード窓(history_limit)を下回らない＝会話文脈は常に同等以上の深さ
        self.assertGreaterEqual(agent_runtime.CONTEXT_HISTORY_LIMIT,
                                agent_runtime.HISTORY_LIMIT)

    def test_deep_enough_for_continuity(self):
        # 数個止まりにしない（会話の連続性を追えるだけ遡る）
        self.assertGreaterEqual(agent_runtime.CONTEXT_HISTORY_LIMIT, 20)


class RunnerDefaultTest(unittest.TestCase):
    """runner経路とセッション継続の既定ON（未指定のときの解決）。

    既定ONにできるのは「使えない環境では静かに旧経路へ寄せる」からで、
    そこが壊れると codex/custom 利用者は全メッセージで回答が落ちる。
    """

    OK = None                     # check_available が None＝使える
    NG = "Codex CLI では次の機能が使えません: Web検索"

    def test_unset_defaults_on_when_available(self):
        self.assertTrue(agent_runtime.resolve_runner_enabled({}, self.OK))

    def test_unset_falls_back_when_unavailable(self):
        self.assertFalse(agent_runtime.resolve_runner_enabled({}, self.NG))

    def test_explicit_true_is_kept_even_if_unavailable(self):
        # 明示指定は尊重する（使えない理由は invoke_claude が出す）
        self.assertTrue(agent_runtime.resolve_runner_enabled(
            {"runner_enabled": True}, self.NG))

    def test_explicit_false_is_kept_even_if_available(self):
        self.assertFalse(agent_runtime.resolve_runner_enabled(
            {"runner_enabled": False}, self.OK))

    def test_session_resume_defaults_on_with_runner(self):
        self.assertTrue(agent_runtime.resolve_session_resume({}, True))

    def test_session_resume_off_without_runner(self):
        # runner が旧経路に落ちたら resume も自動で無効
        self.assertFalse(agent_runtime.resolve_session_resume({}, False))

    def test_session_resume_explicit_false(self):
        self.assertFalse(agent_runtime.resolve_session_resume(
            {"session_resume": {"enabled": False}}, True))


class _FakeAuthor:
    def __init__(self, name, is_bot):
        self.display_name = name
        self.bot = is_bot


class _FakeMsg:
    def __init__(self, name, content, is_bot, id=0):
        self.author = _FakeAuthor(name, is_bot)
        self.clean_content = content
        # 検索から外す範囲の下限に使う（bot.py が最古のIDを拾う）
        self.id = id


class _FakeChannel:
    """discord.TextChannel.history 相当（新しい順に返す）を最小再現。"""

    def __init__(self, newest_first):
        self._msgs = list(newest_first)
        self.calls = []

    def history(self, *, limit=None, before=None):
        self.calls.append({"limit": limit, "before": before})
        msgs = list(self._msgs)

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


class FetchHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_context_limit_and_orders_oldest_first(self):
        channel = _FakeChannel([
            _FakeMsg("アーカイブ担当", " こんばんは ", True, id=30),   # 最新
            _FakeMsg("先輩", None, False, id=20),      # 空content（画像のみ等）
            _FakeMsg("デザイン担当", "図できたよ", True, id=10),       # 最古
        ])
        trigger = _FakeMsg("先輩", "続きは？", False, id=40)
        trigger.channel = channel

        items = await agent_runtime.fetch_history(trigger)

        # 取得件数は「直近の会話」用の広い窓を使う
        self.assertEqual(channel.calls[0]["limit"],
                         agent_runtime.CONTEXT_HISTORY_LIMIT)
        # 自分の発言を含めないよう before=トリガー を渡す
        self.assertIs(channel.calls[0]["before"], trigger)
        # 古い順・content strip・空contentも保持（ループガードに必要）。
        # id は検索の除外範囲を決めるので落とさない
        self.assertEqual(items, [
            {"id": 10, "author": "デザイン担当", "content": "図できたよ",
             "is_bot": True},
            {"id": 20, "author": "先輩", "content": "", "is_bot": False},
            {"id": 30, "author": "アーカイブ担当", "content": "こんばんは",
             "is_bot": True},
        ])

    async def test_history_failure_returns_empty(self):
        class _Boom:
            def history(self, **kw):
                raise RuntimeError("boom")

        trigger = _FakeMsg("先輩", "x", False)
        trigger.channel = _Boom()
        self.assertEqual(await agent_runtime.fetch_history(trigger), [])

