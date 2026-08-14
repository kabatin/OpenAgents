#!/usr/bin/env python3
"""integrations.py（外部連携の登録機構）の単体テスト。

実行: python -m unittest test_integrations -v
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import integrations
def _write(base, name, body):
    """テスト用の連携パッケージを1つ作る。"""
    os.makedirs(os.path.join(base, name), exist_ok=True)
    with open(os.path.join(base, name, "__init__.py"), "w",
              encoding="utf-8") as f:
        f.write(body)


GOOD = '''
NAME = "good"
SKILL_KEY = "good_skill"
SUMMARY = "テスト用"

def skill_note(ctx):
    return f"note for {ctx.agent_id}"

def context_block(ctx, gate_text):
    return "BLOCK" if "見積" in gate_text else None

def apply_markers(ctx, answer):
    return answer.replace("[GOOD]", ""), ["やりました"]

CYCLES = [("good_cycle", lambda ctx: None)]
PREHOOKS = [("good_prehook", lambda ctx: False)]
'''

BROKEN_IMPORT = '''
import this_module_does_not_exist_anywhere
NAME = "broken"
'''

UNAVAILABLE = '''
NAME = "unavailable"
def available(config):
    return False
'''

RAISES = '''
NAME = "raises"
def skill_note(ctx):
    raise RuntimeError("boom")
def apply_markers(ctx, answer):
    raise RuntimeError("boom")
'''

BAD_RETURN = '''
NAME = "badreturn"
def apply_markers(ctx, answer):
    return "只の文字列"
'''


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        _write(self.base, "good", GOOD)
        _write(self.base, "broken", BROKEN_IMPORT)
        _write(self.base, "unavailable", UNAVAILABLE)
        _write(self.base, "raises", RAISES)
        _write(self.base, "badreturn", BAD_RETURN)
        self.ctx = integrations.Context(
            agent_id="a1", agent_name="エージェント", db_path=":memory:")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        for name in ("good", "broken", "unavailable", "raises", "badreturn"):
            sys.modules.pop(name, None)
        if self.base in sys.path:
            sys.path.remove(self.base)

    def _load(self, names):
        return integrations.load({"integrations": {"enabled": names}},
                                 base_dir=self.base)

    def test_有効化したものだけ読み込む(self):
        got = self._load(["good"])
        self.assertEqual([i.name for i in got], ["good"])

    def test_列挙されていない連携は読み込まない(self):
        self.assertEqual(self._load([]), [])

    def test_存在しない名前は無視して落ちない(self):
        got = self._load(["nope", "good"])
        self.assertEqual([i.name for i in got], ["good"])

    def test_import失敗しても他の連携は生きる(self):
        got = self._load(["broken", "good"])
        self.assertEqual([i.name for i in got], ["good"])

    def test_available_がFalseなら無効(self):
        self.assertEqual(self._load(["unavailable"]), [])

    def test_重複指定は1回だけ読む(self):
        got = self._load(["good", "good"])
        self.assertEqual(len(got), 1)

    def test_SKILL_KEY_が既定でNAMEになる(self):
        got = self._load(["raises"])
        self.assertEqual(got[0].skill_key, "raises")


class HookTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        _write(self.base, "good", GOOD)
        _write(self.base, "raises", RAISES)
        _write(self.base, "badreturn", BAD_RETURN)
        self.ctx = integrations.Context(
            agent_id="a1", agent_name="エージェント", db_path=":memory:")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        for name in ("good", "raises", "badreturn"):
            sys.modules.pop(name, None)
        if self.base in sys.path:
            sys.path.remove(self.base)

    def _load(self, names):
        return integrations.load({"integrations": {"enabled": names}},
                                 base_dir=self.base)

    def test_skill_note_を集める(self):
        got = integrations.skill_notes(self._load(["good"]), self.ctx)
        self.assertEqual(got, ["note for a1"])

    def test_context_block_はゲートに掛からなければ出ない(self):
        loaded = self._load(["good"])
        self.assertEqual(
            integrations.context_blocks(loaded, self.ctx, "今日の天気"), [])
        self.assertEqual(
            integrations.context_blocks(loaded, self.ctx, "見積の件"),
            ["BLOCK"])

    def test_apply_markers_が本文と注記を返す(self):
        text, notes = integrations.apply_all_markers(
            self._load(["good"]), self.ctx, "できました[GOOD]")
        self.assertEqual(text, "できました")
        self.assertEqual(notes, ["やりました"])

    def test_フックが例外を投げても本文は壊れない(self):
        loaded = self._load(["raises"])
        self.assertEqual(integrations.skill_notes(loaded, self.ctx), [])
        text, notes = integrations.apply_all_markers(
            loaded, self.ctx, "元の本文")
        self.assertEqual(text, "元の本文")
        self.assertEqual(notes, [])

    def test_apply_markersの戻り値が不正でも本文は壊れない(self):
        text, notes = integrations.apply_all_markers(
            self._load(["badreturn"]), self.ctx, "元の本文")
        self.assertEqual(text, "元の本文")
        self.assertEqual(notes, [])

    def test_CYCLES_と_PREHOOKS_を取り出せる(self):
        got = self._load(["good"])[0]
        self.assertEqual([n for n, _ in got.cycles], ["good_cycle"])
        self.assertEqual([n for n, _ in got.prehooks], ["good_prehook"])

    def test_CYCLES未定義なら空リスト(self):
        got = self._load(["raises"])[0]
        self.assertEqual(got.cycles, [])
        self.assertEqual(got.prehooks, [])


class ForAgentTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        _write(self.base, "good", GOOD)
        self.loaded = integrations.load(
            {"integrations": {"enabled": ["good"]}}, base_dir=self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        sys.modules.pop("good", None)
        if self.base in sys.path:
            sys.path.remove(self.base)

    def test_skills_に書いたエージェントだけ有効(self):
        on = {"id": "a1", "skills": {"good_skill": True}}
        off = {"id": "a2", "skills": {"good_skill": False}}
        none = {"id": "a3"}
        self.assertEqual(len(integrations.for_agent(self.loaded, on)), 1)
        self.assertEqual(integrations.for_agent(self.loaded, off), [])
        self.assertEqual(integrations.for_agent(self.loaded, none), [])

    def test_NAMEではなくSKILL_KEYで判定する(self):
        # skills に NAME("good") を書いても、SKILL_KEY は good_skill なので効かない
        agent = {"id": "a1", "skills": {"good": True}}
        self.assertEqual(integrations.for_agent(self.loaded, agent), [])


class ContextTest(unittest.TestCase):
    def test_Contextはイミュータブル(self):
        ctx = integrations.Context(agent_id="a", agent_name="A", db_path="x")
        with self.assertRaises(Exception):
            ctx.agent_id = "b"

    def test_既定値が入る(self):
        ctx = integrations.Context(agent_id="a", agent_name="A", db_path="x")
        self.assertEqual(ctx.attachments, ())
        self.assertFalse(ctx.is_admin)
        self.assertEqual(ctx.config, {})


if __name__ == "__main__":
    unittest.main()
