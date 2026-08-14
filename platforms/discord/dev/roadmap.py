#!/usr/bin/env python3
"""進化ロードマップのカード運用（バックログのDiscordトリアージ）。

roadmap_seed.json の100案を費用対効果スコア順に1枚ずつAI開発室へ提案し、
管理者の 👍=実装（route=devbot は起票化・session はセッション行き）
👎=見送り で流していく。判断は常に人間、進行は開発BOT（bot.py）が担う。

責務はデータ処理（seed・選定・CAS判定・整形）に絞り、Discord投稿・
ジョブ起動は bot.py 側が持つ。単体テスト: test_roadmap.py
"""

import datetime
import json
import os

from core import db

HERE = os.path.dirname(os.path.abspath(__file__))

SEED_PATH = os.path.join(HERE, "roadmap_seed.json")

TIER_LABEL = {"quiet": "静か系（新規投稿なし）",
              "outward": "外向き（新しい発言が増える）",
              "risky": "慎重枠"}
ROUTE_LABEL = {"devbot": "あたし（開発BOT）",
               "session": "管理者セッション向け"}


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def seed(db_path, path=SEED_PATH):
    """シードを投入（冪等。既存idの進行状態は上書きしない）。新規件数を返す。"""
    with open(path, encoding="utf-8") as f:
        items = json.load(f)["items"]
    added = 0
    now = _now_iso()
    with db.connect(db_path) as conn:
        for it in items:
            if db.roadmap_seed_item(
                    conn, id=int(it["id"]), title=it["title"],
                    description=it["desc"], category=it["cat"],
                    tier=it["tier"], route=it["route"],
                    effect=int(it["effect"]), cost=int(it["cost"]),
                    created_at=now):
                added += 1
    return added


def stars(n):
    n = max(0, min(5, int(n)))
    return "★" * n + "☆" * (5 - n)


def format_card(item):
    """提案カードの文面（純粋関数・つむぎの声）。"""
    route = ROUTE_LABEL.get(item["route"], item["route"])
    tier = TIER_LABEL.get(item["tier"], item["tier"])
    return (
        f"🗺️ **ロードマップ #{item['id']}**【{item['category']}】"
        f"{item['title']}\n"
        f"> {item['description']}\n"
        f"効果{stars(item['effect'])} × 手間{stars(item['cost'])}"
        f" ｜ {tier} ｜ 実装: {route}\n"
        "👍=実装する / 👎=見送る、で教えてくださいね！（進捗は `!roadmap`）"
    )


def pick_next(db_path):
    """次に提案すべき項目。提案中カードが既にあれば None（1枚ずつ運用）。"""
    with db.connect(db_path) as conn:
        if db.roadmap_proposed(conn) is not None:
            return None
        return db.roadmap_next_pending(conn)


def mark_proposed(db_path, item_id, message_id):
    with db.connect(db_path) as conn:
        return db.roadmap_mark_proposed(conn, item_id, message_id, _now_iso())


def decide(db_path, message_id, approve, admin_id):
    """カードへの👍👎を処理（CASで排他）。

    Returns: None（対象カードでない/競合負け） または
      {"item":…, "action": "start_devbot"|"queued_session"|"skipped",
       "cap_req_id": int|None}
    approve かつ route=devbot のときは capability_requests へ起票する。
    """
    with db.connect(db_path) as conn:
        item = db.roadmap_by_message(conn, message_id)
        if item is None:
            return None
        return _decide_item(conn, item, approve, admin_id)


def decide_current(db_path, approve, admin_id):
    """!roadmap 要約への👍👎を「いま提案中のカード」への決定として扱う。
    提案中カードは常に1枚（1枚ずつ運用）なので対象は一意に定まる。"""
    with db.connect(db_path) as conn:
        item = db.roadmap_proposed(conn)
        if item is None:
            return None
        return _decide_item(conn, item, approve, admin_id)


def _decide_item(conn, item, approve, admin_id):
    """決定の本体（decide / decide_current 共通。CASで二重決定を排他）。"""
    now = _now_iso()
    if not approve:
        if not db.roadmap_claim_decision(conn, item["id"], "skipped", now):
            return None
        return {"item": item, "action": "skipped", "cap_req_id": None}
    to_status = ("approved" if item["route"] == "devbot"
                 else "queued_session")
    if not db.roadmap_claim_decision(conn, item["id"], to_status, now):
        return None
    cap_id = None
    if item["route"] == "devbot":
        cap_id = db.add_capability_request(
            conn, agent_id="roadmap",
            description=f"[RM#{item['id']}] {item['title']} — "
                        f"{item['description']}",
            context=f"進化ロードマップ #{item['id']}"
                    f"（{item['category']}/{item['tier']}）を管理者が👍承認",
            requested_by=str(admin_id),
            source_msg_id=item["card_message_id"],
            created_at=now)
        db.roadmap_set_cap(conn, item["id"], cap_id)
    action = "start_devbot" if cap_id else "queued_session"
    return {"item": item, "action": action, "cap_req_id": cap_id}


def progress_summary(db_path, guild_id=None, channel_id=None):
    """!roadmap 用の進捗テキスト。guild_id/channel_id を渡すと提案中カードへの
    ジャンプリンクを載せる（要約に👍👎した人がカードへ飛べるように）。"""
    with db.connect(db_path) as conn:
        counts = db.roadmap_counts(conn)
        proposed = db.roadmap_proposed(conn)
        queue = db.roadmap_session_queue(conn)
    total = sum(counts.values())
    done = counts.get("approved", 0) + counts.get("queued_session", 0) \
        + counts.get("skipped", 0) + counts.get("done", 0)
    lines = [f"🗺️ ロードマップ進捗: 判断済み {done}/{total} 件"
             f"（実装承認 {counts.get('approved', 0)}"
             f"・セッション行き {counts.get('queued_session', 0)}"
             f"・実装完了 {counts.get('done', 0)}"
             f"・見送り {counts.get('skipped', 0)}"
             f"・未提案 {counts.get('pending', 0)}）"]
    if proposed is not None:
        line = f"いま提案中: #{proposed['id']} {proposed['title']}"
        if guild_id and channel_id and proposed.get("card_message_id"):
            link = (f"https://discord.com/channels/{guild_id}/{channel_id}/"
                    f"{proposed['card_message_id']}")
            line += (f"\n→ カード: {link}\n"
                     "-# このメッセージに👍/👎してもらってもカードへの判断として扱いますね")
        else:
            line += "（👍/👎待ちです）"
        lines.append(line)
    if queue:
        heads = "、".join(f"#{q['id']} {q['title']}" for q in queue[:5])
        lines.append(f"セッション行きの積み残し: {heads}"
                     + ("…" if len(queue) > 5 else ""))
    return "\n".join(lines)


# ------------------------------------------- 起票の自動拾い上げ（RM#21）

# checkpoint（どの起票idまで見たか）を proactive_state に相乗りさせるキー
CAPWATCH_STATE_KEY = "capwatch:devbot"

AGENT_LABEL = {"agent1": "エージェント1", "agent2": "エージェント2",
               "agent3": "エージェント3"}


def watch_next_cap(db_path):
    """新しいオーガニック起票を1件返す（無ければ/提案中があれば None）。
    初回はcheckpointを現在の最大起票idに初期化して None を返す
    （導入前の古い起票をいきなり蒸し返さない）。"""
    now = _now_iso()
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, CAPWATCH_STATE_KEY)
        if state is None:
            db.set_proactive_state(
                conn, CAPWATCH_STATE_KEY,
                last_checked_message_id=db.max_capability_request_id(conn),
                last_run_at=now)
            return None
        if db.cap_proposal_pending(conn):
            return None    # 1件ずつ（👍👎待ちがあるうちは次を出さない）
        return db.next_unproposed_capability(
            conn, state["last_checked_message_id"] or 0)


def mark_cap_proposed(db_path, cap_req_id, message_id):
    with db.connect(db_path) as conn:
        db.add_cap_proposal(conn, cap_req_id=cap_req_id,
                            message_id=message_id, created_at=_now_iso())


def format_cap_proposal(cap):
    """起票提案の文面（純粋関数・つむぎの声）。"""
    who = AGENT_LABEL.get(cap["agent_id"], f"{cap['agent_id']}さん")
    return (
        f"🧵 {who}が新しい起票を出しています！\n"
        f"**起票#{cap['id']}**: {cap['description'][:200]}\n"
        "あたしが着手していいですか？ 👍=着手 / 👎=見送り"
    )


def decide_cap(db_path, message_id, approve):
    """起票提案への👍👎（CAS排他）。Returns: None（対象外/競合負け）または
    {"cap_id": int, "approved": bool}。👎は起票をrejectedで閉じる。"""
    now = _now_iso()
    with db.connect(db_path) as conn:
        cap_id = db.cap_proposal_by_message(conn, message_id)
        if cap_id is None:
            return None
        to_status = "accepted" if approve else "declined"
        if not db.claim_cap_proposal(conn, cap_id, to_status, now):
            return None
        state = db.get_proactive_state(conn, CAPWATCH_STATE_KEY)
        last = (state or {}).get("last_checked_message_id") or 0
        db.set_proactive_state(conn, CAPWATCH_STATE_KEY,
                               last_checked_message_id=max(last, cap_id),
                               last_run_at=now)
        if not approve:
            db.set_capability_status(conn, cap_id, "rejected")
        return {"cap_id": cap_id, "approved": approve}
