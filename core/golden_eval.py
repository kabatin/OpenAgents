#!/usr/bin/env python3
"""ゴールデンセット回帰評価（v4 Phase 4・Phase 0 の物差し）。

模範Q&A（golden_set。人が採用した curated、無ければ👍自動捕獲の auto）を今の回答経路で
答え直し、参照回答と比べて採点する。プロンプト・注入・ツール構成を変える前後で走らせ、
劣化していないかを見る（Step D の A/B の物差し）。書き込みツールは出さない
（dry_run）ので本番DBは汚れない。

使い方（repo ルートで）:
  ./venv/bin/python -m core.golden_eval --n 8            # 8問を評価（既定は全件）
  ./venv/bin/python -m core.golden_eval --no-tools       # ツールループ無し（ベースライン）
  ./venv/bin/python -m core.golden_eval --seed 1 --n 8   # 同じ抽出で再実行（比較用）
  ./venv/bin/python -m core.golden_eval --inject-search 0 --no-inject-facts --prompt v4
結果は state/golden_eval/<日時>.json と標準出力の要約。週次レポートが最新の
JSON（mean / scored）を読む。
コスト目安: 1問あたり回答 $0.15〜0.3 ＋ 採点（haiku）$0.01。"""

import argparse
import json
import os
import random
import re
import time

from core import config as app_config
from core import db
from core import honesty
from core import invoke_claude
from core import paths
from core import reminders
from core import search

GRADER_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
OUT_DIR = paths.GOLDEN_EVAL_DIR
_JSON_RE = re.compile(r"\{.*\}", re.S)


def select_rows(rows, n=None, seed=None):
    """評価対象を選ぶ（純粋関数）。seed 指定で再現可能な抽出。"""
    rows = list(rows)
    if seed is not None:
        random.Random(seed).shuffle(rows)
    return rows[:n] if n else rows


def history_for(conn, channel_id, answer_message_id, limit=10):
    """ゴールデン行の「回答直前の会話」をアーカイブから復元する（search.build_history 互換）。
    golden_set には Q/A しか無いが、👍が付いた回答は会話の流れの中で成立している
    （「登録しておきます」等）。質問だけで答え直すと参照と噛み合わないので文脈を戻す。"""
    if not channel_id or not answer_message_id:
        return []
    rows = conn.execute(
        """SELECT u.display_name, u.is_bot, m.content FROM messages m
             LEFT JOIN users u ON u.id = m.author_id
            WHERE m.channel_id=? AND m.id<? AND m.deleted=0
            ORDER BY m.id DESC LIMIT ?""",
        (channel_id, answer_message_id, limit + 1)).fetchall()
    hist = [{"author": r[0] or "?", "is_bot": bool(r[1]), "content": r[2] or ""}
            for r in reversed(rows)]
    # 末尾＝質問そのもの（golden の question）なので履歴からは外す
    return hist[:-1] if hist else []


# 「〜しておきます」「〜しとくね」など、これから実行する約束の言い回し
_PROMISE_RE = re.compile(
    r"(?:し(?:とき|ておき)?ます|(?:しとく|しておく|する|やっとく)(?:ね|よ))")


def classify_reference(reference):
    """参照回答の種別（純粋関数）。
    info   = 情報の回答（比較採点の対象）
    action = 実行結果（-# 行つき）や「〜しておきます」の約束。状態が動く操作なので、
             後日答え直しても DB が変わっていて噛み合わない。参考扱いで平均から外す。"""
    text = reference or ""
    if any(rx.search(text) for rx in honesty.SUCCESS_DEEDS.values()) or \
            any(rx.search(text) for rx in honesty.FAIL_DEEDS.values()):
        return "action"
    head = text.strip().split("\n")[0][:80]
    if _PROMISE_RE.search(head) and len(text) < 200:
        return "action"
    return "info"


def build_grade_prompt(question, reference, candidate):
    """採点プロンプト（純粋関数）。参照＝人間が👍した回答。"""
    return (
        "社内AIアシスタントの回答を、人間が良いと評価した参照回答と比べて採点して。\n\n"
        f"【質問】\n{(question or '')[:600]}\n\n"
        f"【参照回答（人間が👍）】\n{(reference or '')[:1200]}\n\n"
        f"【新しい回答】\n{(candidate or '')[:1200]}\n\n"
        "採点（合計0〜5）: 事実の一致 0-2（参照と矛盾するチーム内の事実があれば0）/ "
        "根拠リンクや出典の有無 0-1 / 質問への的中 0-1 / 簡潔さ・トーン 0-1。\n"
        "参照より良い点があれば加点してよいが、参照に無いチーム内の事実を断定していたら"
        "事実の一致は0。参照が「〜しておきます」のような実行の約束なら、新回答が同じ"
        "行為を実際に行った／確認した証拠（-# 行やツール結果）があれば同等以上とみなす。\n"
        "出力はJSONのみ: {\"score\": 0-5, \"note\": \"一言\"}"
    )


def parse_grade(raw):
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        score = float(data.get("score"))
    except (ValueError, TypeError):
        return None
    if not 0 <= score <= 5:
        return None
    return {"score": score, "note": str(data.get("note") or "")[:120]}


def _agent_param(agent, tools):
    from core.archive_tools import launch
    persona = [paths.resolve(p) for p in (agent.get("persona_files") or [])]
    role = agent.get("role") or ""
    if tools:
        role += "\n" + launch.skill_note(False)
    return {"name": agent["name"], "persona_files": persona, "role": role}


def evaluate(db_path, agent, rows, *, tools=True, answer_model=None,
             grader_model=GRADER_MODEL_DEFAULT, guild_id="0",
             answer_fn=None, grade_fn=None, log=print,
             inject_search_hits=None, inject_facts=True, prompt_style="v3"):
    """各行を答え直して採点する。answer_fn/grade_fn はテスト差し替え口。
    Returns: {"items": [...], "mean": float, "n": int, "scored": int, ...}"""
    from core import runner_answer
    from core.archive_tools import launch
    from core.archive_tools.context import ToolContext

    items = []
    for row in rows:
        started = time.monotonic()
        try:
            if answer_fn is not None:
                answer, meta = answer_fn(row)
            else:
                kw = {}
                if tools:
                    ctx = ToolContext(
                        agent_id=agent["id"], actor_id="0", db_path=db_path,
                        guild_id=guild_id, channel_id=row.get("channel_id"),
                        message_id=0, dry_run=True,
                        skills=frozenset({"reminder", "action_tracking"}))
                    plan = launch.build(ctx)
                    kw = {"mcp_config": plan.mcp_config,
                          "mcp_allow": plan.allow, "max_budget_usd": 0.5,
                          "inject_search_hits": inject_search_hits,
                          "inject_facts": inject_facts,
                          "prompt_style": prompt_style}
                with db.connect(db_path) as conn:
                    history = history_for(conn, row.get("channel_id"),
                                          row.get("source_answer_id"))
                res = runner_answer.answer_question(
                    db_path, guild_id, row["question"],
                    answer_model or search.DEFAULT_MODEL,
                    row.get("channel_id"), history,
                    _agent_param(agent, tools), **kw)
                answer, meta = res["answer"], res.get("meta") or {}
            grade_raw = (grade_fn(row, answer) if grade_fn else
                         invoke_claude.invoke(
                             build_grade_prompt(row["question"], row["answer"],
                                                answer),
                             model=grader_model, timeout=90,
                             purpose="golden_grade").text)
            grade = parse_grade(grade_raw) or {"score": None,
                                               "note": "採点不能"}
            items.append({
                "golden_id": row["id"], "kind": classify_reference(row["answer"]),
                "question": row["question"][:200],
                "answer": answer[:1500], "score": grade["score"],
                "note": grade["note"], "tool_calls": meta.get("tool_calls"),
                "tools_used": meta.get("tools_used"),
                "cost_usd": meta.get("cost_usd"),
                "duration_ms": meta.get("duration_ms"),
                "elapsed_s": round(time.monotonic() - started, 1)})
            log(f"[{row['id']}] {items[-1]['kind']} score={grade['score']} "
                f"tools={meta.get('tool_calls')} {grade['note']}")
        except Exception as e:  # noqa: BLE001 - 1問の失敗で全体を止めない
            items.append({"golden_id": row["id"], "score": None,
                          "note": f"error: {e}"[:200]})
            log(f"[{row['id']}] error: {e}")
    scored = [i["score"] for i in items
              if i.get("score") is not None and i.get("kind") == "info"]
    mean = round(sum(scored) / len(scored), 2) if scored else None
    action_n = sum(1 for i in items if i.get("kind") == "action")
    return {"items": items, "mean": mean, "n": len(items),
            "scored": len(scored), "action_excluded": action_n, "tools": tools,
            "variant": {"inject_search_hits": inject_search_hits,
                        "inject_facts": inject_facts,
                        "prompt_style": prompt_style}}


def save_report(report, out_dir=None, now=None):
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = (now or reminders.now_jst()).strftime("%Y%m%d-%H%M")
    path = os.path.join(out_dir, f"{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return path


def load_rows(conn, agent_id, golden_set="curated", kind="all"):
    """評価対象の行を選ぶ（curated が無ければ auto へ倒す）。"""
    if golden_set == "curated":
        rows = db.golden_rows(conn, statuses=("curated",))
        if not rows:
            print("curated が無いので auto（👍自動捕獲）を使う")
            rows = db.golden_rows(conn)
    elif golden_set == "auto":
        rows = db.golden_rows(conn)
    else:
        rows = db.golden_rows(conn, statuses=("active", "curated"))
    rows = [r for r in rows if r["agent_id"] == agent_id]
    if kind != "all":
        rows = [r for r in rows if classify_reference(r["answer"]) == kind]
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--agent", default=None,
                    help="エージェントid（既定: config の先頭）")
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--model", default=None, help="回答モデル（既定 search.DEFAULT_MODEL）")
    ap.add_argument("--grader", default=GRADER_MODEL_DEFAULT)
    ap.add_argument("--kind", choices=["info", "action", "all"], default="all",
                    help="info=情報の回答だけ（推奨）/ action=実行系だけ / all")
    ap.add_argument("--set", choices=["curated", "auto", "all"], default="curated",
                    help="curated=人が採用した模範Q&A（既定・無ければ auto へ）/ auto=👍自動捕獲 / all")
    ap.add_argument("--inject-search", type=int, default=None,
                    help="事前注入する関連メッセージ上限（既定24・0で注入なし）")
    ap.add_argument("--no-inject-facts", action="store_true")
    ap.add_argument("--prompt", choices=["v3", "v4"], default="v3")
    args = ap.parse_args(argv)

    cfg = app_config.load()
    agents = cfg.get("agents") or []
    agent = (next((a for a in agents if a["id"] == args.agent), None)
             if args.agent else (agents[0] if agents else None))
    if agent is None:
        raise SystemExit("エージェントが見つかりません（config.json の agents）")
    db_path = paths.DB_PATH
    with db.connect(db_path) as conn:
        rows = load_rows(conn, agent["id"], args.set, args.kind)
    rows = select_rows(rows, n=args.n, seed=args.seed)
    print(f"golden[{args.set}] {len(rows)}問 / tools={'off' if args.no_tools else 'on'} / "
          f"inject={args.inject_search if args.inject_search is not None else 24}"
          f"{'' if not args.no_inject_facts else ',no-facts'} / prompt={args.prompt} / "
          f"model={args.model or search.DEFAULT_MODEL}")
    report = evaluate(db_path, agent, rows, tools=not args.no_tools,
                      answer_model=args.model, grader_model=args.grader,
                      guild_id=str(cfg.get("guild_id") or "0"),
                      inject_search_hits=args.inject_search,
                      inject_facts=not args.no_inject_facts,
                      prompt_style=args.prompt)
    report["config"] = {"n": args.n, "seed": args.seed, "agent": agent["id"],
                        "model": args.model or search.DEFAULT_MODEL}
    path = save_report(report)
    cost = sum(i.get("cost_usd") or 0 for i in report["items"])
    print(f"mean(info)={report['mean']} ({report['scored']} 採点・action "
          f"{report['action_excluded']}件は参考) "
          f"cost=${cost:.2f} → {path}")


if __name__ == "__main__":
    main()
