#!/usr/bin/env python3
"""ツールループの bot 側配線（ToolLoopMixin）のユニットテスト。
Discord クライアントは作らず、SimpleNamespace の self で mixin を直接呼ぶ。"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from core import db
from core import honesty
from core.archive_tools import launch
from platforms.discord import agent_loops
from platforms.discord import bot
from platforms.discord import marker_actions
from platforms.discord import tool_loop


def _tool_use(tid, name, inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _tool_result(tid, payload):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": json.dumps(payload)}]}]}}


def _client(cfg, *, runner=True, reminder=True, tracking=False, archiver=False):
    """ToolLoopMixin の self 相当（bot.py の属性だけを持つ）。"""
    c = tool_loop.ToolLoopMixin.__new__(tool_loop.ToolLoopMixin)
    c.agent = {"id": "agent1", "reminder_max_active": 5}
    c.runner_enabled = runner
    c.tool_loop_cfg = launch.normalize(cfg)
    c.reminder = reminder
    c.action_tracking = tracking
    c.is_archiver = archiver
    return c


def _msg(author_id=100, bot_author=False, text="納期教えて"):
    return NS(id=555, clean_content=text, channel=NS(id=200),
              author=NS(id=author_id, bot=bot_author))


class ToolLoopGatesTest(unittest.TestCase):
    def test_off_by_default_and_shadow(self):
        c = _client(None)
        self.assertFalse(c._tool_loop_on())
        self.assertFalse(c._tool_loop_live())
        c = _client({"enabled": True})
        self.assertTrue(c._tool_loop_on())
        self.assertFalse(c._tool_loop_live())   # 既定シャドー
        c = _client({"enabled": True, "shadow": False})
        self.assertTrue(c._tool_loop_live())
        c = _client({"enabled": True, "shadow": False}, runner=False)
        self.assertFalse(c._tool_loop_on())     # runner 経路が前提

    def test_skills_follow_agent_flags(self):
        c = _client({"enabled": True}, reminder=True, tracking=True,
                    archiver=True)
        self.assertEqual(c._tool_skills(),
                         frozenset({"reminder", "action_tracking", "quota"}))
        c = _client({"enabled": True}, reminder=False)
        self.assertEqual(c._tool_skills(), frozenset())

    def test_context_from_message(self):
        c = _client({"enabled": True})
        with patch.object(tool_loop, "ADMIN_IDS", {"100"}), \
                patch.object(tool_loop, "AGENTS", [{"id": "agent1"},
                                                   {"id": "agent2"}]):
            ctx = c._tool_context(_msg(author_id=100))
        self.assertEqual(ctx.actor_id, "100")
        self.assertTrue(ctx.is_admin)
        self.assertTrue(ctx.dry_run)          # シャドー＝読み取りのみ
        self.assertFalse(ctx.bot_turn)
        self.assertEqual(ctx.reminder_max_active, 5)
        self.assertEqual(ctx.agent_ids, ("agent1", "agent2"))
        self.assertEqual(ctx.question, "納期教えて")
        ctx = c._tool_context(_msg(author_id=7, bot_author=True))
        self.assertTrue(ctx.bot_turn)
        self.assertFalse(ctx.is_admin)

    def test_observe_kwargs_only_when_enabled(self):
        c = _client(None)
        self.assertEqual(c._observe_tool_kwargs(actor_id=1, channel_id=2,
                                                message_id=3), {})
        c = _client({"enabled": True, "shadow": False, "prompt_style": "v4",
                     "inject_search_hits": 0})
        kw = c._observe_tool_kwargs(actor_id=1, channel_id=2, message_id=3)
        self.assertIn("mcpServers", kw["mcp_config"])
        self.assertTrue(all(a.startswith("mcp__archive__") for a in kw["mcp_allow"]))
        # 観察ループは常に読み取りのみ（本番設定でも write は見せない）
        self.assertFalse(any("save_" in a or "add_" in a for a in kw["mcp_allow"]))
        self.assertEqual(kw["prompt_style"], "v4")
        self.assertEqual(kw["inject_search_hits"], 0)

    def test_agent_loops_fallback_without_bot_mixin(self):
        # AgentLoopsMixin 単体（bot 側の mixin が無い）ではツール無し
        loops = agent_loops.AgentLoopsMixin.__new__(agent_loops.AgentLoopsMixin)
        self.assertEqual(loops._observe_tool_kwargs(actor_id=1, channel_id=2,
                                                    message_id=3), {})


class ToolEvidenceTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        p = patch.object(tool_loop, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        os.unlink(self.db_path)

    def _log_rows(self):
        with db.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT kind, action, detail FROM proactive_log ORDER BY id"
            ).fetchall()

    def test_unused_is_recorded_and_answer_untouched(self):
        c = _client({"enabled": True, "shadow": False})
        out = c._apply_tool_evidence(_msg(), "答え", [])
        self.assertEqual(out, "答え")
        self.assertEqual([(r[0], r[1]) for r in self._log_rows()],
                         [("tool_loop", "unused")])

    def test_shadow_records_only(self):
        c = _client({"enabled": True, "shadow": True})
        events = [_tool_use("a", "mcp__archive__search_messages", {}),
                  _tool_result("a", {"ok": True, "hits": 0})]
        out = c._apply_tool_evidence(_msg(), "答え", events)
        self.assertEqual(out, "答え")            # シャドーは本文に付けない
        rows = self._log_rows()
        self.assertEqual((rows[0][0], rows[0][1]), ("tool_loop", "shadow"))
        self.assertIn("search_messages", rows[0][2])

    def test_live_appends_notes_and_failure_first(self):
        c = _client({"enabled": True, "shadow": False})
        events = [
            _tool_use("a", "mcp__archive__add_reminder", {"content": "x"}),
            _tool_result("a", {"ok": True, "evidence": "-# 登録: id=1 明日9時 x"}),
            _tool_use("b", "mcp__archive__update_task", {"key": "A6"}),
            _tool_result("b", {"ok": False, "error": "担当外",
                               "evidence": "-# ⚠️ 納期追跡: 担当外"}),
        ]
        out = c._apply_tool_evidence(_msg(), "登録しました", events)
        self.assertTrue(out.startswith("⚠️ 一部の操作が失敗しました（"))
        self.assertIn("-# 登録: id=1", out)
        self.assertIn("-# ⚠️ 納期追跡: 担当外", out)
        self.assertEqual(self._log_rows()[0][1], "used")

    def test_live_dedupes_notes_already_in_body(self):
        c = _client({"enabled": True, "shadow": False})
        events = [
            _tool_use("a", "mcp__archive__add_reminder", {}),
            _tool_result("a", {"ok": True, "evidence": "-# 登録: id=1"}),
        ]
        out = c._apply_tool_evidence(_msg(), "登録しました\n-# 登録: id=1", events)
        self.assertEqual(out.count("-# 登録: id=1"), 1)

    def test_denied_tools_recorded_separately(self):
        c = _client({"enabled": True, "shadow": False})
        events = [{"type": "result", "permission_denials": [
            {"tool_name": "mcp__archive__save_rule"}]}]
        out = c._apply_tool_evidence(_msg(), "答え", events)
        self.assertIn("権限外の操作", out)
        kinds = [r[0] for r in self._log_rows()]
        self.assertIn("tool_denied", kinds)


class HonestyByToolsTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        p = patch.object(marker_actions, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        os.unlink(self.db_path)

    def _self(self):
        c = marker_actions.MarkerActionsMixin.__new__(
            marker_actions.MarkerActionsMixin)
        c.agent = {"id": "agent1"}
        c.integrations = ()
        c.action_tracking = False
        return c

    def test_claim_without_tool_is_fake_with_tools_used(self):
        c = self._self()
        text = "明日9時にリマインダーを登録しました！"
        self.assertTrue(honesty.detect_fake_done_by_tools(text, []))
        out = c._apply_honesty_check(_msg(), text, 5, tools_used=[])
        self.assertTrue(out.startswith("-# ⚠️") or "⚠️" in out.split("\n")[0])
        # 対応ツールを呼んでいれば -# 行が無くても嘘扱いしない
        out = c._apply_honesty_check(_msg(), text, 5,
                                     tools_used=["add_reminder"])
        self.assertEqual(out, text)


class BotWiringTest(unittest.TestCase):
    def test_agent_client_has_tool_loop_mixin(self):
        self.assertTrue(issubclass(bot.AgentClient, tool_loop.ToolLoopMixin))
        self.assertTrue(issubclass(bot.AgentClient,
                                   agent_loops.AgentLoopsMixin))
        # bot 側の実装が mixin 経由で観察ループにも届く（MRO で ToolLoopMixin が先）
        mro = bot.AgentClient.__mro__
        self.assertLess(mro.index(tool_loop.ToolLoopMixin),
                        mro.index(agent_loops.AgentLoopsMixin))

    def test_respond_disables_resume_while_tool_loop_on(self):
        import inspect
        src = inspect.getsource(bot.AgentClient._respond)
        self.assertIn("and not tool_loop_on", src)
        self.assertIn("strip_retired_markers", src)
        self.assertIn("dump_trace", src)


if __name__ == "__main__":
    unittest.main()
