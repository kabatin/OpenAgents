#!/usr/bin/env python3
"""エピソード記憶（進化ロードマップ#3）＋人の得意分野マップ（RM#53）。

thread_summaries が「今の文脈」なのに対し、episodes は「何がいつ起きたか」の
履歴。決定台帳・完了タスク・イベント承認という**既にある構造化データ**を
時系列に積むだけなので、LLM呼び出しゼロ・決定論で作れる。

用途:
  - 回答Contextに「このチャンネルのこれまで」を注入（起きた順が分かる）
  - 「あの件どうなったっけ」に履歴で答えられる
  - #53: profilesを集約した「誰に何を聞けばいいか」マップも同じ思想の集約

単体テスト: ./venv/bin/python -m unittest test_batch_pack3 -v
"""

import os


from core import db
from core import reminders
KIND_LABELS = {"decision": "決定", "done": "完了", "event": "イベント確定"}
MAX_IN_CONTEXT = 8
MAX_SUMMARY = 100


def sync(db_path, limit=50):
    """決定・完了タスク・承認イベントを episodes へ取り込む（冪等・決定論）。
    Returns: 追加件数。"""
    now = reminders.fmt(reminders.now_jst())
    added = 0
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, channel_id, decided_on, decision FROM decisions
               WHERE status='active' ORDER BY id DESC LIMIT ?""",
            (limit,)).fetchall()
        for did, ch, on, text in rows:
            if ch and not db.episode_exists(conn, "decision", did):
                db.add_episode(conn, channel_id=ch, happened_on=on or now[:10],
                               kind="decision", summary=(text or "")[:MAX_SUMMARY],
                               source_ref=did, created_at=now)
                added += 1
        rows = conn.execute(
            """SELECT id, channel_id, due_date, task FROM action_items
               WHERE status='done' ORDER BY id DESC LIMIT ?""",
            (limit,)).fetchall()
        for aid, ch, on, task in rows:
            if ch and not db.episode_exists(conn, "done", aid):
                db.add_episode(conn, channel_id=ch, happened_on=on or now[:10],
                               kind="done", summary=(task or "")[:MAX_SUMMARY],
                               source_ref=aid, created_at=now)
                added += 1
        rows = conn.execute(
            """SELECT id, channel_id, event_date, name FROM events
               WHERE status='planned' ORDER BY id DESC LIMIT ?""",
            (limit,)).fetchall()
        for eid, ch, on, name in rows:
            if ch and not db.episode_exists(conn, "event", eid):
                db.add_episode(conn, channel_id=ch, happened_on=on or now[:10],
                               kind="event", summary=(name or "")[:MAX_SUMMARY],
                               source_ref=eid, created_at=now)
                added += 1
    return added


def build_timeline_block(db_path, channel_id):
    """回答Contextに載せる出来事タイムライン（無ければ空文字）。"""
    with db.connect(db_path) as conn:
        rows = db.channel_episodes(conn, channel_id, limit=MAX_IN_CONTEXT)
    if not rows:
        return ""
    lines = [f"- {r['happened_on']} [{KIND_LABELS.get(r['kind'], r['kind'])}]"
             f" {r['summary']}" for r in rows]
    return ("【このチャンネルのこれまでの経緯（新しい順・記録から自動生成）】\n"
            + "\n".join(lines))


# ------------------------------------------- 人の得意分野マップ（RM#53）

def build_expertise_map(db_path, limit=12):
    """profilesを集約した「誰に何を聞けばいいか」（無ければ空文字）。
    プロファイルの1行目（役割行）だけを引用して簡潔に保つ。"""
    with db.connect(db_path) as conn:
        rows = db.all_profiles(conn, limit=limit)
    lines = []
    for r in rows:
        first = ""
        for ln in (r["profile"] or "").splitlines():
            ln = ln.strip().lstrip("-・ ").strip()
            if ln:
                first = ln[:60]
                break
        if first:
            lines.append(f"- {r['display_name']}: {first}")
    if not lines:
        return ""
    return ("【メンバーの担当・得意分野（過去の会話からの推定。"
            "『誰に聞けばいいか』の案内に使う。断定はしない）】\n"
            + "\n".join(lines))
