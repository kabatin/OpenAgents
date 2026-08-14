#!/usr/bin/env python3
"""回答生成の2経路が同じ呼び出し方を受け付けることの検査。

bot.py は `search.answer_question` と `runner_answer.answer_question` を
フラグ1つで呼び分ける（runner_enabled）。両者のシグネチャがズレると、
**フラグを切り替えた瞬間だけ** TypeError で落ちる。テストが通っていても
本番の片方の経路だけが壊れる、という一番見つけにくい事故になるので、
契約が一致していることをここで機械的に確かめる。

実行: ./venv/bin/python -m unittest test_answer_contract -v
"""

import ast
import inspect
import os
import unittest

from platforms.discord import bot
from core import runner_answer
from core import search

HERE = os.path.dirname(os.path.abspath(__file__))


def _kwargs(fn):
    return {n for n, p in inspect.signature(fn).parameters.items()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}


class SignatureContractTest(unittest.TestCase):
    def test_runner経路は旧経路の引数をすべて受け付ける(self):
        legacy = _kwargs(search.answer_question)
        runner = _kwargs(runner_answer.answer_question)
        missing = legacy - runner
        self.assertEqual(missing, set(),
                         f"runner_answer に無い引数: {missing}")

    def test_runner専用の追加引数は既知のものだけ(self):
        # 追加は許すが、意図せず増えていないことを見る（契約の見張り）
        extra = _kwargs(runner_answer.answer_question) - _kwargs(
            search.answer_question)
        self.assertEqual(extra, {"resume", "session_cwd"})

    def test_両経路とも外部連携のブロックを受け取れる(self):
        for fn in (search.answer_question, runner_answer.answer_question):
            self.assertIn("extra_blocks", _kwargs(fn), fn.__module__)


class CallSiteContractTest(unittest.TestCase):
    """bot.py が実際に渡しているキーワード引数が、両経路に存在すること。

    シグネチャ同士の比較だけでは「呼び出し側が使う名前」を保証できない。
    bot.py を構文解析して answer_fn(...) の呼び出しを拾い、キーワード名が
    両方の関数に存在することを確かめる。
    """

    def _answer_fn_call_keywords(self):
        """bot.py の `asyncio.to_thread(answer_fn, ...)` からキーワード名を拾う。

        回答生成はブロッキング処理なので、必ず to_thread 経由で呼ばれる。
        第1引数が answer_fn の呼び出しだけを対象にする。
        """
        with open(os.path.join(HERE, "bot.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "answer_fn":
                found.append({kw.arg for kw in node.keywords if kw.arg})
        return found

    def test_呼び出しが見つかる(self):
        calls = self._answer_fn_call_keywords()
        self.assertGreater(len(calls), 0,
                           "bot.py に answer_fn(...) の呼び出しが無い")

    def test_渡しているキーワードが両経路に存在する(self):
        legacy = _kwargs(search.answer_question)
        runner = _kwargs(runner_answer.answer_question)
        for keywords in self._answer_fn_call_keywords():
            # resume/session_cwd は **extra 展開で runner 経路にだけ渡る
            self.assertTrue(keywords <= runner,
                            f"runner経路に無い引数: {keywords - runner}")
            self.assertTrue(keywords <= legacy | {"resume", "session_cwd"},
                            f"旧経路に無い引数: {keywords - legacy}")


class IntegrationsUsageTest(unittest.TestCase):
    """外部連携が入っていない既定状態で、関連処理が全て素通りすること。"""

    def test_連携ゼロなら空リストになる(self):
        from core import integrations
        self.assertEqual(
            integrations.for_agent([], {"id": "a", "skills": {"x": True}}), [])

    def test_連携ゼロならブロックも注記も出ない(self):
        from core import integrations
        ctx = integrations.Context(agent_id="a", agent_name="A", db_path="x")
        self.assertEqual(integrations.skill_notes([], ctx), [])
        self.assertEqual(integrations.context_blocks([], ctx, "何か"), [])
        text, notes = integrations.apply_all_markers([], ctx, "本文")
        self.assertEqual((text, notes), ("本文", []))


if __name__ == "__main__":
    unittest.main()
