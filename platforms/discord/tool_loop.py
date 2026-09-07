#!/usr/bin/env python3
"""ツールループ（v4 Phase 1）の bot 側の糊。AgentClient に混ぜる mixin。

回答経路（bot._respond）と観察ループ（agent_loops）が、archive ツール
（core/archive_tools）を claude -p に渡すための組み立てをここに集める:
  - 文脈（ToolContext）の組み立て: 権限は発言者の id から、見せるツールはスキルから
  - 進捗ログ（tool_use / MCP 接続状態）
  - 証拠行（-# 行）の付与と proactive_log への記録（シャドー/本番/未使用/権限外）
設定は agents[].tool_loop（launch.normalize 済みの self.tool_loop_cfg）。
"""

from core import proactive
from core.archive_tools import evidence as tool_evidence
from core.archive_tools import launch as tool_launch
from core.archive_tools import registry as tool_registry
from core.archive_tools.context import ToolContext
from platforms.discord.agent_runtime import ADMIN_IDS, AGENTS, DB_PATH, GUILD_ID


class ToolLoopMixin:
    """self.agent / self.runner_enabled / self.tool_loop_cfg /
    self.reminder / self.action_tracking / self.is_archiver を参照する。"""

    def _tool_loop_on(self):
        """ツールループを使うか（runner 経路が前提）。"""
        return bool(getattr(self, "runner_enabled", False)
                    and self.tool_loop_cfg["enabled"])

    def _tool_loop_live(self):
        """本番（write ツールあり・マーカー実行は停止）か。"""
        return self._tool_loop_on() and not self.tool_loop_cfg["shadow"]

    def _tool_skills(self):
        """archive ツールの表示を絞るスキル集合（RBAC はコード）。"""
        return frozenset(name for name, on in (
            ("reminder", getattr(self, "reminder", False)),
            ("action_tracking", getattr(self, "action_tracking", False)),
            # 自発発言枠の変更はアーカイブ担当（マネージャ）だけ
            ("quota", getattr(self, "is_archiver", False)))
            if on)

    def _tool_context(self, message):
        return ToolContext(
            agent_id=self.agent["id"], actor_id=str(message.author.id),
            db_path=DB_PATH, guild_id=str(GUILD_ID),
            channel_id=message.channel.id, message_id=message.id,
            bot_turn=bool(message.author.bot),
            dry_run=bool(self.tool_loop_cfg["shadow"]),
            is_admin=str(message.author.id) in ADMIN_IDS,
            skills=self._tool_skills(),
            reminder_max_active=self.agent.get("reminder_max_active"),
            question=(message.clean_content or "")[:500],
            agent_ids=tuple(a["id"] for a in AGENTS))

    def _observe_tool_kwargs(self, *, actor_id, channel_id, message_id):
        """観察ループ用のツールループ引数（読み取りのみ＝dry_run）。
        tool_loop が無効なら {}。回答経路と同じ archive ツールで裏付けを引き直せる。"""
        if not self._tool_loop_on():
            return {}
        ctx = ToolContext(
            agent_id=self.agent["id"], actor_id=str(actor_id or 0),
            db_path=DB_PATH, guild_id=str(GUILD_ID),
            channel_id=int(channel_id) if channel_id else None,
            message_id=int(message_id) if message_id else None,
            dry_run=True, skills=self._tool_skills())
        plan = tool_launch.build(ctx)
        cfg = self.tool_loop_cfg
        return {"mcp_config": plan.mcp_config, "mcp_allow": plan.allow,
                "max_budget_usd": cfg["max_budget_usd"],
                "inject_search_hits": cfg["inject_search_hits"],
                "inject_facts": cfg["inject_facts"],
                "prompt_style": cfg["prompt_style"]}

    def _tool_progress(self, ev):
        """ツール呼び出しの進捗（worker スレッドから呼ばれる・ログのみ）。
        init イベントの MCP 接続状態も出す（繋がっていないのに黙って
        ツール無しで答える事故を bot.log から追えるように）。"""
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            for srv in ev.get("mcp_servers") or []:
                print(f"[{self.agent['id']}] mcp: {srv.get('name')} "
                      f"{srv.get('status')}")
            return
        if ev.get("type") != "assistant":
            return
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                print(f"[{self.agent['id']}] tool: {b.get('name')}")

    def _apply_tool_evidence(self, message, answer, events):
        """ツール結果から -# 行を決定論で作る。シャドーでは記録だけ、本番では
        本文に付ける。失敗した write があれば失敗を1行目に置く。
        権限外の呼び出し（permission_denials）は RBAC の穴の検出器として別に記録する。"""
        used = tool_evidence.tools_used(events)
        notes, failed = tool_evidence.build_notes(events)
        notes = tool_evidence.dedupe_notes(answer, notes)
        shadow = bool(self.tool_loop_cfg["shadow"])
        denied = tool_evidence.denied_tools(events)
        if denied:
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="tool_denied", action="denied",
                channel_id=message.channel.id,
                trigger_message_id=message.id, detail=",".join(denied)[:300])
        if not (used or notes):
            # 使えたのに使わなかった回答も記録する（使用率＝Step D の物差し）
            proactive.log_entry(
                DB_PATH, self.agent["id"], kind="tool_loop", action="unused",
                channel_id=message.channel.id,
                trigger_message_id=message.id, detail=None)
            return answer
        detail = ",".join(used)
        if notes:
            detail += " | " + " / ".join(notes)
        proactive.log_entry(
            DB_PATH, self.agent["id"], kind="tool_loop",
            action="shadow" if shadow else "used",
            channel_id=message.channel.id,
            trigger_message_id=message.id, detail=detail[:300])
        if shadow or not notes:
            return answer
        body = (answer + "\n" if answer else "") + "\n".join(notes)
        if failed:
            labels = [tool_registry.label_of(n) for n in failed]
            body = ("⚠️ 一部の操作が失敗しました（" + "、".join(labels)
                    + "）\n\n" + body)
        return body
