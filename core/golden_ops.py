#!/usr/bin/env python3
"""操作の回帰チェック（週次評価の「頼まれごと」側）。

週次評価（golden_eval）は「過去ログを思い出して答える質問」しか採点して
いないが、実際の依頼はリマインダー・単語帳・訂正・ルールといった操作が
多い。評価の文脈は書き込みツールを見せないので、操作が壊れても点数に出ない。

ここではよくある頼み方をもとにした問題を答えさせ、「正しいツールを
正しい引数で呼んだか」をコードで判定する（採点にAIは使わない）。
書き込みツールは予行（ToolContext.rehearse）で動かすので、何も変わらない。
"""

import re
import time
from datetime import timedelta

from core import paths
from core import reminders
from core import search
from core.archive_tools import evidence, registry

# 「30分後」の期待時刻に許すずれ（回答に数十秒かかり、分の切り上げ方も揺れる）
NEAR_TOLERANCE_MIN = 5
# これ以上落ちたら「点検で赤」（失敗の台帳へ → 開発BOTへ起票）
RED_FAILS = 2

# 1問＝よくある頼み方を、状態（登録済みのid等）に依らない形にしたもの。
#   expect: 呼ばれているべきツールと引数の条件（順不同・ほかの呼び出しは許す）
#   no_write: 書き込みツールを1つも呼んではいけない（見るだけの依頼）
#   admin: 管理者からの依頼として答えさせる
CASES = (
    {"id": "remind-tomorrow",
     "question": "明日の15時にリマインドして。請求書の確認",
     "expect": [("add_reminder", {"due": "{d1} 15:00",
                                  "content": {"has": "請求書"}})]},
    {"id": "remind-30min",
     "question": "30分後にリマインドして。会議室の予約",
     "expect": [("add_reminder", {"due": {"near_min": 30},
                                  "content": {"has": "会議室"}})]},
    {"id": "remind-weekly",
     "question": "毎週水曜の10時に「備品の在庫チェック」ってリマインドして",
     "expect": [("add_reminder", {"repeat": "weekly",
                                  "content": {"has": "備品"},
                                  "due": {"re": r"10:00$"}})]},
    {"id": "remind-list", "question": "リマインドリスト",
     "expect": [("list_reminders", {})], "no_write": True},
    {"id": "glossary-two",
     "question": "アーカイプ：アーカイブ\nダッシュボート：ダッシュボード\n単語帳に登録して",
     "expect": [("save_glossary", {"wrong": "アーカイプ", "correct": "アーカイブ"}),
                ("save_glossary", {"wrong": "ダッシュボート",
                                   "correct": "ダッシュボード"})]},
    {"id": "fact-correction",
     "question": "定例は今週から金曜じゃなくて木曜の18時開始に変わったよ",
     "expect": [("save_fact", {"fact": {"has": "木曜"}})]},
    {"id": "rule-from-now-on",
     "question": "今後、告知文の下書きには絵文字を使わないで",
     "expect": [("save_rule", {"text": {"has": "絵文字"}})]},
)


def _dates(now):
    return {"d0": now.strftime("%Y-%m-%d"),
            "d1": (now + timedelta(days=1)).strftime("%Y-%m-%d")}


def _matches(cond, value, now):
    """引数1つの条件を満たすか（純粋関数）。
    文字列＝一致（{d0}/{d1} は今日/明日の日付）／has＝含む／re＝正規表現／
    near_min＝今から約N分後の日時。"""
    text = "" if value is None else str(value).strip()
    if isinstance(cond, str):
        return text == cond.format(**_dates(now))
    if "has" in cond:
        return cond["has"] in text
    if "re" in cond:
        return re.search(cond["re"], text) is not None
    if "near_min" in cond:
        try:
            due = reminders.parse_dt(text)
        except ValueError:
            return False
        gap = abs((due - now).total_seconds() / 60 - cond["near_min"])
        return gap <= NEAR_TOLERANCE_MIN
    raise ValueError(f"unknown condition: {cond}")


def check(case, calls, now):
    """1問の判定（純粋関数）。calls＝[{"name", "input"}]（呼ばれた順）。
    Returns: {"pass": bool, "why": 落ちた理由の一言}"""
    names = [c["name"] for c in calls]
    if case.get("no_write"):
        wrote = [n for n in names if n in registry.write_tool_names()]
        if wrote:
            return {"pass": False, "why": f"見るだけの依頼で {wrote[0]} を呼んだ"}
    for tool, conds in case["expect"]:
        mine = [c["input"] or {} for c in calls if c["name"] == tool]
        if not mine:
            return {"pass": False, "why": f"{tool} を呼ばなかった"}
        if not any(all(_matches(cond, args.get(key), now)
                       for key, cond in conds.items()) for args in mine):
            return {"pass": False,
                    "why": f"{tool} の引数が違う: {str(mine[0])[:120]}"}
    return {"pass": True, "why": ""}


def applicable(cases, visible):
    """このエージェントに見えているツールだけで解ける問題に絞る（純粋関数）。
    リマインダーのスキルを持たないエージェントに登録を頼んでも、できないのが正しい。"""
    return tuple(c for c in cases
                 if all(tool in visible for tool, _ in c["expect"]))


def summary(items):
    return {"passed": sum(1 for i in items if i.get("pass")), "n": len(items)}


def red(items):
    """点検で赤にするか（純粋関数）。赤なら説明文、揺れの範囲なら None。
    1問だけの失敗は答えの揺れでも起きるので、結果に残すだけで起票はしない。"""
    failed = [i for i in items if not i.get("pass")]
    if len(failed) < RED_FAILS:
        return None
    return (f"頼まれごとの操作 {len(items) - len(failed)}/{len(items)}問\n"
            + "\n".join(f"{i['id']}: {str(i.get('why') or '')[:120]}"
                        for i in failed[:4]))


def evaluate(db_path, agent, cfg, *, cases=CASES, answer_model=None,
             answer_fn=None, now_fn=reminders.now_jst, log=print):
    """各問を答えさせて判定する。answer_fn(case) -> events はテスト差し替え口。
    本番（answer_fn なし）では、このエージェントに見えないツールが要る問題は外す。
    Returns: {"items": [...], "passed": int, "n": int}"""
    if answer_fn is None:
        cases = applicable(cases, visible_names(db_path, agent, cfg))
    items = []
    for case in cases:
        started, now = time.monotonic(), now_fn()
        try:
            got = ({"events": answer_fn(case)} if answer_fn else _answer(
                db_path, agent, cfg, case, answer_model, now))
            calls = evidence.tool_results(got.get("events") or [])
            res = check(case, calls, now)
            items.append({"id": case["id"], **res,
                          "tools_used": [c["name"] for c in calls],
                          "answer": (got.get("answer") or "")[:300],
                          "cost_usd": got.get("cost_usd"),
                          "elapsed_s": round(time.monotonic() - started, 1)})
        except Exception as e:  # noqa: BLE001 - 1問の失敗で全体を止めない
            items.append({"id": case["id"], "pass": False,
                          "why": f"error: {e}"[:200]})
        log(f"[{case['id']}] {'ok' if items[-1]['pass'] else 'NG'} "
            f"{items[-1]['why']}")
    return {"items": items, **summary(items)}


def _context(db_path, agent, guild_id, channel_id=None, case=None):
    """予行の文脈。書き込みツールは見えるが、呼んでも実行されない。"""
    from core.archive_tools import launch
    from core.archive_tools.context import ToolContext
    case = case or {}
    return ToolContext(
        agent_id=agent["id"], actor_id="0", db_path=db_path,
        guild_id=guild_id, channel_id=channel_id, message_id=0,
        rehearse=True, is_admin=bool(case.get("admin")),
        skills=launch.skills_for(agent), question=case.get("question") or "")


def visible_names(db_path, agent, cfg):
    """このエージェントの予行で見えるツール名。"""
    registry.ensure_loaded()
    ctx = _context(db_path, agent, str(cfg.get("guild_id") or "0"))
    return {t.name for t in registry.visible_tools(ctx)}


def _answer(db_path, agent, cfg, case, answer_model, now):
    """本番と同じ構成で1問答えさせる。書き込みは予行なので何も変わらない。"""
    from core import runner_answer
    from core.archive_tools import launch

    tl = launch.normalize(agent.get("tool_loop"))
    channel_id = int(agent.get("home_channel_id") or 0) or None
    guild_id = str(cfg.get("guild_id") or "0")
    plan = launch.build(_context(db_path, agent, guild_id, channel_id, case))
    persona = [paths.resolve(p) for p in (agent.get("persona_files") or [])]
    res = runner_answer.answer_question(
        db_path, guild_id, case["question"],
        answer_model or search.DEFAULT_MODEL, channel_id, [],
        {"name": agent["name"], "persona_files": persona,
         "role": (agent.get("role") or "") + "\n" + launch.skill_note(True),
         "context": launch.now_note(now)},
        mcp_config=plan.mcp_config, mcp_allow=plan.allow,
        max_budget_usd=tl["max_budget_usd"],
        inject_search_hits=tl["inject_search_hits"],
        inject_facts=tl["inject_facts"], prompt_style=tl["prompt_style"])
    return {"events": res.get("events"), "answer": res.get("answer"),
            "cost_usd": (res.get("meta") or {}).get("cost_usd")}
