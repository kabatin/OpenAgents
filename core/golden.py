#!/usr/bin/env python3
"""ゴールデンセット自動拡充（進化ロードマップ#16）。

👍がついた実際のQ&Aペアを評価セットとして自動蓄積する。将来プロンプトや
モデルを変えるときの回帰テスト資産（v2設計「検証済み置換」の土台）。
蓄積は静かなデータ（投稿を増やさない）。評価実行側（!eval的なもの）は
セットが溜まってからの別ロードマップ。

単体テスト: ./venv/bin/python -m unittest test_golden -v
"""

from core import db
from core import reminders

MAX_LEN = 2000   # 保存するQ/Aの上限（肥大防止）


def capture(db_path, agent_id, answer_message_id):
    """👍がついたエージェント回答をQ&Aとして保存。質問は回答直前の人間発言を
    充てる（回答は_send_chunked投稿でreferenceを持たないための近似）。
    保存したら True（重複・材料不足は False）。"""
    with db.connect(db_path) as conn:
        ans = db.get_message(conn, answer_message_id)
        if ans is None or not (ans.get("content") or "").strip():
            return False
        question = db.preceding_human_message(
            conn, ans["channel_id"], answer_message_id)
        if not question:
            return False
        return db.add_golden(
            conn, agent_id=agent_id, question=question[:MAX_LEN],
            answer=ans["content"][:MAX_LEN],
            source_answer_id=answer_message_id,
            channel_id=ans["channel_id"],
            created_at=reminders.fmt(reminders.now_jst()))
