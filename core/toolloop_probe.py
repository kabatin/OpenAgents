#!/usr/bin/env python3
"""ツールループの回帰プローブ（煙テスト）。

実際の claude -p で archive-tools（MCP）を叩き、①サーバが接続される ②ツールが
呼ばれる ③結果が返る、を確認する。claude CLI の更新で MCP の挙動が変わった時に
気づくための最小の煙テスト（Haiku・読み取りツールのみ・数セント）。

  ./venv/bin/python -m core.toolloop_probe            # 本番 DB を読む（書き込みは無い）
  ./venv/bin/python -m core.toolloop_probe --db path  # 別の DB で
終了コード 0=OK / 1=NG。ユニットテストでは実行しない（claude CLI と費用が要る）。"""

import argparse
import sys

from core import config as app_config
from core import invoke_claude
from core import paths
from core.archive_tools import launch
from core.archive_tools.context import ToolContext

PROBE_MODEL = "claude-haiku-4-5-20251001"
QUESTION = ("追跡中のタスクは何件？ 社内ログで「議事録」に触れた投稿は何件？ "
            "それぞれツールで確認して数字だけ短く報告して。")


def mcp_status(events):
    for ev in events:
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            return {s.get("name"): s.get("status")
                    for s in ev.get("mcp_servers") or []}
    return {}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=paths.DB_PATH)
    ap.add_argument("--model", default=PROBE_MODEL)
    args = ap.parse_args(argv)
    cfg = app_config.load()
    agent_id = (cfg.get("agents") or [{}])[0].get("id") or "agent1"
    ctx = ToolContext(agent_id=agent_id, actor_id="0", db_path=args.db,
                      guild_id=str(cfg["guild_id"]), channel_id=None,
                      message_id=0, dry_run=True,
                      skills=frozenset({"reminder", "action_tracking"}))
    plan = launch.build(ctx)
    res = invoke_claude.invoke(
        "【質問】\n" + QUESTION, model=args.model, mcp_config=plan.mcp_config,
        allow=plan.allow, max_budget_usd=0.3, timeout=180, purpose="probe",
        system=launch.skill_note(False))
    status = mcp_status(res.events)
    used = [n.replace(launch.qualified(""), "")
            for n in res.meta.get("tools_used", [])]
    ok = (status.get(launch.SERVER_NAME) == "connected" and bool(used)
          and (res.meta.get("denials") or 0) == 0 and bool(res.text.strip()))
    print(f"mcp={status} tools_used={used} turns={res.meta.get('num_turns')} "
          f"cost=${res.meta.get('cost_usd') or 0:.3f} denials={res.meta.get('denials')}")
    print("answer:", res.text[:160].replace("\n", " / "))
    print("PROBE", "OK" if ok else "NG")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
