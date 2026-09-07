#!/usr/bin/env python3
"""ゴールデンのキュレーション（模範Q&Aの候補生成 → 人が採用）。

👍自動捕獲の golden_set は「登録します」型や当時の状態に依存する回答が多く、
回帰テストの物差しにならなかった。代わりに、決定台帳・事実台帳・固有名詞辞書から
「時間が経っても答えが変わらない社内Q&A」の候補を作り、人が採用/不採用を決める。

  ./venv/bin/python -m core.golden_curate --propose --n 20    # 候補を作って candidate で保存
  ./venv/bin/python -m core.golden_curate --list              # 候補と採用済みの一覧
  ./venv/bin/python -m core.golden_curate --approve 40,41,45  # 採用（curated）
  ./venv/bin/python -m core.golden_curate --reject 42,43      # 不採用（rejected）
候補作成は LLM を1回呼ぶ。採用分（status=curated）は回帰評価の物差しに使う。"""

import argparse
import json
import re

from core import config as app_config
from core import db
from core import invoke_claude
from core import paths
from core import reminders
from core import search
from core import textsim

_JSON_RE = re.compile(r"\[.*\]", re.S)
MAX_SOURCES = 70
PROPOSE_TIMEOUT_SEC = 300


def gather_sources(conn, guild_id):
    """候補の材料（決定・事実・固有名詞）を出典リンクつきで集める（決定は新しい順）。"""
    out = []
    rows = conn.execute(
        """SELECT id, decision, topic, source_message_id, channel_id, decided_on
             FROM decisions WHERE status='active' ORDER BY id DESC LIMIT ?""",
        (MAX_SOURCES,)).fetchall()
    for r in rows:
        link = (search.jump_link(guild_id, r[4], r[3])
                if r[3] and r[4] else "")
        out.append({"kind": "decision", "id": r[0], "topic": r[2] or "",
                    "text": r[1], "date": r[5] or "", "link": link})
    for r in conn.execute(
            """SELECT id, topic, fact, source_message_id, channel_id, stated_by
                 FROM facts WHERE status='active' ORDER BY id DESC LIMIT 20"""):
        link = (search.jump_link(guild_id, r[4], r[3])
                if r[3] and r[4] else "")
        out.append({"kind": "fact", "id": r[0], "topic": r[1] or "",
                    "text": r[2], "date": "", "link": link,
                    "by": r[5] or ""})
    for t in db.terms_all(conn):
        out.append({"kind": "term", "id": 0, "topic": t.get("term") or "",
                    "text": t.get("description") or "", "date": "", "link": ""})
    return out


def build_prompt(sources, n, existing_questions=()):
    lines = []
    for s in sources:
        tag = {"decision": "決定", "fact": "事実", "term": "用語"}[s["kind"]]
        date = f"（{s['date']}）" if s.get("date") else ""
        link = f" link={s['link']}" if s.get("link") else ""
        lines.append(f"- [{tag}#{s['id']}] {s['topic']}: {s['text']}{date}{link}")
    ex = ""
    if existing_questions:
        ex = ("\n\n【既にある質問（重複を避ける）】\n"
              + "\n".join(f"- {q}" for q in existing_questions))
    return (
        "社内チャットのアシスタントAIの回帰テスト用に、模範Q&Aを作ってください。\n"
        "条件:\n"
        "- 時間が経っても答えが変わらない社内の事実（日程・場所・担当・決定・"
        "正式名称・数値）だけを対象にする。リマインダーや進行中タスクの状態、"
        "「今の一覧」は対象外\n"
        "- 質問は社員が実際に聞きそうな自然な日本語（1文）。答えは丁寧体で"
        "2文以内、出典 link があれば末尾に「参照: <link>」を付ける\n"
        "- 材料に無い事実を足さない。材料の同じ主題から複数作らない\n"
        f"- {n}問。JSON配列のみ出力: "
        '[{"question": "...", "answer": "...", "source": "決定#12", "link": "..."}]\n\n'
        "【材料】\n" + "\n".join(lines) + ex)


def parse(raw):
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        a = str(it.get("answer") or "").strip()
        if q and a:
            out.append({"question": q[:300], "answer": a[:800],
                        "source": str(it.get("source") or "")[:40],
                        "link": str(it.get("link") or "")[:200]})
    return out


def propose(db_path, agent_id, *, n=20, model=None, guild_id="0",
            invoke_fn=None, now=None):
    """候補を作って candidate として保存。保存した行のリストを返す。"""
    with db.connect(db_path) as conn:
        sources = gather_sources(conn, guild_id)
        existing = db.golden_rows(conn, statuses=("candidate", "curated"))
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model or search.DEFAULT_MODEL,
        timeout=PROPOSE_TIMEOUT_SEC, purpose="golden_curate").text)
    items = parse(fn(build_prompt(sources, n,
                                  [r["question"] for r in existing])))
    saved = []
    stamp = reminders.fmt(now or reminders.now_jst())
    with db.connect(db_path) as conn:
        known = [r["question"] for r in existing]
        for it in items:
            if textsim.find_same(it["question"], known):
                continue
            gid = db.add_golden_candidate(
                conn, agent_id=agent_id, question=it["question"],
                answer=it["answer"], source_link=it["link"] or None,
                note=it["source"] or None, created_at=stamp)
            known.append(it["question"])
            saved.append({"id": gid, **it})
    return saved


def set_status(db_path, ids, status):
    with db.connect(db_path) as conn:
        for gid in ids:
            db.set_golden_status(conn, int(gid), status)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--approve", default="")
    ap.add_argument("--reject", default="")
    ap.add_argument("--agent", default=None,
                    help="候補を紐づけるエージェントid（既定: 先頭のエージェント）")
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)
    cfg = app_config.load()
    guild_id = str(cfg["guild_id"])
    agent_id = args.agent or cfg["agents"][0]["id"]
    db.init_db(paths.DB_PATH)   # kind/source_link/note 列の追加（冪等）
    if args.propose:
        saved = propose(paths.DB_PATH, agent_id, n=args.n, model=args.model,
                        guild_id=guild_id)
        print(f"候補 {len(saved)} 件を保存")
        for s in saved:
            print(f"  #{s['id']} {s['question']}")
    if args.approve:
        set_status(paths.DB_PATH, args.approve.split(","), "curated")
        print("採用:", args.approve)
    if args.reject:
        set_status(paths.DB_PATH, args.reject.split(","), "rejected")
        print("不採用:", args.reject)
    if args.list or not (args.propose or args.approve or args.reject):
        with db.connect(paths.DB_PATH) as conn:
            rows = db.golden_rows(conn, statuses=("candidate", "curated",
                                                  "rejected"))
        for r in rows:
            print(f"#{r['id']} [{r['status']}] {r['question']}\n"
                  f"    → {r['answer'][:100]}")


if __name__ == "__main__":
    main()
