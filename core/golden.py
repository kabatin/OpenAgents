#!/usr/bin/env python3
"""ゴールデンセット自動拡充（進化ロードマップ#16）。

👍がついた実際のQ&Aペアを評価セットとして自動蓄積する。将来プロンプトや
モデルを変えるときの回帰テスト資産（v2設計「検証済み置換」の土台）。
蓄積は静かなデータ（投稿を増やさない）。評価実行側は必要になった時点で作る
（いまはプロンプト変更が頻繁で回帰実行の運用コストが見合わないため保留）。

**入口の品質ゲート（2026-08-18）**: 質問は「回答直前の人間発言」という近似で
充てているが、一方的な投稿（週次レポート・リマインド配信・自発発言・宿題
催促・ニュース）に👍が付くと、質問が存在しないため無関係な発言を拾って
Q&Aペアが壊れる。次を捕獲対象から外す:
  - proactive_log に載っている投稿（＝一方的な発信）
  - 定時投稿・配信の書式で始まる投稿（週次レポート「📊」やリマインド配信「⏰」
    等はログに載らない経路で出るため、本文の書式でも判定する）
  - 褒め言葉・相槌だけの「質問」（評価データにならない）

単体テスト: ./venv/bin/python -m unittest core.test_golden -v
"""

import re

from core import db
from core import reminders

MAX_LEN = 2000   # 保存するQ/Aの上限（肥大防止）
MIN_QUESTION_LEN = 8    # これ未満は相槌とみなす
# 褒め言葉・相槌の判定（質問として成立しないもの）
PRAISE_RE = re.compile(
    r"^(?:おお+|わ+ー?|さすが|偉い|えらい|ナイス|nice|good|いいね|"
    r"良いね|よき|助かる|ありがとう|ありがと|thx|加点|草|笑)+[!!。、…\s]*$")


# 定時投稿・自動配信の書式（proactive_log に載らない経路の投稿を本文で判定）。
# 例: 週次レポート・リマインド配信・自己監査・新聞・組織図
AUTO_POST_RE = re.compile(
    r"^(?:📊|⏰|🌙|🗞|🗂|⚖️|📜|📚|📰|🩺|📋|🌊|💭|🎓|✉️)")


def is_auto_post(answer):
    """定時投稿・自動配信の本文か（純粋関数）。質問への回答ではない。"""
    return bool(AUTO_POST_RE.match((answer or "").lstrip()))


def is_praise_only(text):
    """褒め言葉・相槌だけか（純粋関数）。質問として成立しないので除外する。"""
    t = " ".join((text or "").split())
    if not t or len(t) < MIN_QUESTION_LEN:
        return True
    return bool(PRAISE_RE.match(t))


def capture(db_path, agent_id, answer_message_id):
    """👍がついたエージェント回答をQ&Aとして保存。質問は回答直前の人間発言を
    充てる（回答は_send_chunked投稿でreferenceを持たないための近似）。
    一方的な投稿・相槌のみの質問は評価データにならないので保存しない。
    保存したら True（重複・材料不足・品質ゲート落ちは False）。"""
    with db.connect(db_path) as conn:
        ans = db.get_message(conn, answer_message_id)
        if ans is None or not (ans.get("content") or "").strip():
            return False
        if db.is_unsolicited_post(conn, answer_message_id) \
                or is_auto_post(ans["content"]):
            return False   # レポート/リマインド/自発発言＝質問が存在しない
        question = db.preceding_human_message(
            conn, ans["channel_id"], answer_message_id)
        if not question or is_praise_only(question):
            return False
        return db.add_golden(
            conn, agent_id=agent_id, question=question[:MAX_LEN],
            answer=ans["content"][:MAX_LEN],
            source_answer_id=answer_message_id,
            channel_id=ans["channel_id"],
            created_at=reminders.fmt(reminders.now_jst()))


def audit_existing(db_path):
    """既存行を仕分ける（第2段）。一方的投稿由来・相槌質問の行を無効化する。
    Returns: {"disabled": [(id, 理由)], "kept": 件数}"""
    disabled = []
    with db.connect(db_path) as conn:
        for row in db.golden_rows(conn, active_only=True):
            reason = None
            if db.is_unsolicited_post(conn, row["source_answer_id"]):
                reason = "一方的投稿（質問なし）"
            elif is_auto_post(row["answer"]):
                reason = "定時投稿・自動配信の書式"
            elif is_praise_only(row["question"]):
                reason = "相槌・褒め言葉のみ"
            if reason:
                db.set_golden_status(conn, row["id"], "invalid")
                disabled.append((row["id"], reason))
        kept = db.count_golden(conn, active_only=True)
    return {"disabled": disabled, "kept": kept}
