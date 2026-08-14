#!/usr/bin/env python3
"""未回答質問の救済（進化ロードマップ#31）。

24時間以上誰も答えていない質問を検出し、アーカイブ担当が拾って答える。

三段ゲート（誤救済＝「答え済みへの二重回答」の防止）:
  1) 決定論の候補抽出: 疑問符つき・24〜72時間経過・リプライゼロ・未判定
     （db.unanswered_question_candidates。SQLで絞ってからしか claude を呼ばない）
  2) LLM判定（安いモデル）: 質問後のチャンネルの流れを見て「実質答えられて
     いないか」「拾う価値のある実質的な質問か」を判定
  3) シャドー既定: 実投稿せず proactive_log に記録だけ（rescue.shadow=false で
     本投稿を解禁）。本投稿は既存の回答パイプラインで生成し、日次枠を消費する

単体テスト: ./venv/bin/python -m unittest test_rescue -v
"""

import json
import os
import re
from datetime import datetime, timedelta

from core import invoke_claude
from core import db
from core import reminders
WINDOW_MIN_HOURS = 24    # これより新しい質問は「まだ誰か答えるかも」なので待つ
WINDOW_MAX_HOURS = 72    # これより古い話は蒸し返さない
JUDGE_TIMEOUT_SEC = 120
FOLLOWUP_MESSAGES = 15   # 判定に渡す「質問の後の流れ」の件数
_JSON_RE = re.compile(r"\{.*\}", re.S)

# 修辞疑問・雑談の粗除去（LLM判定前の追加フィルタ。保守的に短文の相槌だけ弾く）
_NOISE_RE = re.compile(r"^(?:え|ほんと|まじ|マジ|そうなの|なるほど)[？?！!〜ー]*$")


def _utc_iso(dt):
    return dt.isoformat(timespec="seconds")


def find_candidates(db_path, *, exclude_channel_ids=(), now=None, limit=3):
    """救済候補（古い順）。now はUTCのdatetime（テスト注入用）。"""
    now = now or datetime.utcnow()
    since = _utc_iso(now - timedelta(hours=WINDOW_MAX_HOURS))
    until = _utc_iso(now - timedelta(hours=WINDOW_MIN_HOURS))
    with db.connect(db_path) as conn:
        rows = db.unanswered_question_candidates(
            conn, since=since, until=until,
            exclude_channel_ids=exclude_channel_ids, limit=limit)
    return [r for r in rows
            if not _NOISE_RE.match((r["content"] or "").strip())]


def followups(db_path, candidate):
    """質問の後に同じchで交わされた発言（判定材料）。"""
    with db.connect(db_path) as conn:
        return db.messages_after(conn, candidate["channel_id"],
                                 after_id=candidate["id"],
                                 limit=FOLLOWUP_MESSAGES)


def build_judge_prompt(candidate, follow_rows):
    """救済判定プロンプト（純粋関数・テスト対象）。"""
    lines = []
    for r in follow_rows:
        text = (r.get("content") or "").strip().replace("\n", " ")[:200]
        if text:
            lines.append(f"- {r.get('author') or '?'}: {text}")
    flow = "\n".join(lines) or "（その後の発言なし）"
    return (
        "チャットで24時間以上前に投稿された次の発言が、"
        "「まだ答えられていない実質的な質問」かどうかを判定して。\n\n"
        f"【質問候補】#{candidate['channel']} {candidate['author']}:\n"
        f"「{(candidate['content'] or '').strip()[:400]}」\n\n"
        f"【その後の同チャンネルの流れ】\n{flow}\n\n"
        "判定基準:\n"
        "- その後の流れの中で実質的に答えられている/解決している → 救済不要\n"
        "- 修辞疑問・独り言・雑談・呼びかけ → 救済不要\n"
        "- 特定の個人への私的な問いかけ → 救済不要（本人に任せる）\n"
        "- 情報を求める実質的な質問で、誰も答えていない → 救済する\n"
        "迷ったら救済しない（誤った拾い上げは信頼を失う）。\n"
        "出力はJSONのみ: {\"rescue\": true|false, \"reason\": \"一言\"}"
    )


def parse_judge(raw):
    """判定JSONの解釈（純粋関数）。壊れていたら救済しない＝安全側。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return {"rescue": False, "reason": "判定不能"}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {"rescue": False, "reason": "判定不能"}
    return {"rescue": bool(data.get("rescue")),
            "reason": str(data.get("reason") or "")[:200]}


def judge(candidate, follow_rows, *, model, invoke_fn=None):
    """LLM判定: 救済すべきか。invoke_fnはテスト差し替え口。"""
    prompt = build_judge_prompt(candidate, follow_rows)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=JUDGE_TIMEOUT_SEC).text)
    return parse_judge(fn(prompt))


RESCUE_PREFIX = ("24時間くらい誰も答えてないみたいなので、"
                 "あたしなりに調べてみたっス🙋\n")


def record(db_path, message_id, agent_id, status, posted_message_id=None):
    with db.connect(db_path) as conn:
        db.add_rescue(conn, message_id=message_id, agent_id=agent_id,
                      status=status, posted_message_id=posted_message_id,
                      created_at=reminders.fmt(reminders.now_jst()))
