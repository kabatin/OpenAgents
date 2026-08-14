#!/usr/bin/env python3
"""生きた社内Wiki（とっておき#103）。

トピックの「今の正」（確定情報・担当・期日・出典リンク）を1本の投稿に編纂し、
関連する新しい決定が入ったら**その投稿を編集で更新**し続ける（組織図と同じ
編集上書き方式＝チャットを流さない）。ピン留めすれば常設の答えの場所になる。

- 作成: 会話で頼まれたらLLMが [WIKI: トピック] マーカーを出し、コードが編纂・投稿
- 更新: 観察サイクルで各ページごとに「前回以降の新決定がトピックに触れるか」を
  決定論で先読みし、触れるときだけ再編纂（LLMコストは変化があった時だけ）

単体テスト: ./venv/bin/python -m unittest test_treasure_pack -v
"""

import os
import re

from core import invoke_claude
from core import db
from core import reminders
from core import search
WIKI_MARKER_RE = re.compile(r"\[WIKI:\s*([^\]]+)\]")
MAX_TOPIC_LEN = 40
MAX_PAGE_CHARS = 1800
COMPILE_TIMEOUT_SEC = 300
_TERM_RE = re.compile(r"[ァ-ヶー一-龠a-zA-Z0-9]{2,}")


def extract_markers(answer):
    """[WIKI: トピック] を全て除去し、(本文, トピック[]) を返す。"""
    topics = []
    for m in WIKI_MARKER_RE.finditer(answer or ""):
        t = m.group(1).strip()[:MAX_TOPIC_LEN]
        if t and t not in topics:
            topics.append(t)
    return WIKI_MARKER_RE.sub("", answer or "").strip(), topics


def topic_terms(topic):
    return _TERM_RE.findall(topic or "")[:4] or [topic]


def gather(db_path, topic):
    """編纂の材料（決定・用語・過去ログ）。"""
    terms = topic_terms(topic)
    with db.connect(db_path) as conn:
        decisions = db.search_decisions(conn, terms, limit=10)
        all_terms = db.terms_all(conn)
    glossary_rows = [t for t in all_terms
                     if any(w in t["term"] or w in (t["description"] or "")
                            for w in terms)]
    messages = search.search_messages(db_path, terms, limit=10)
    return {"decisions": decisions, "terms": glossary_rows,
            "messages": messages}


def build_compile_prompt(topic, material, guild_id, today):
    dec_lines = []
    for d in material["decisions"]:
        link = ""
        if d.get("channel_id") and d.get("source_message_id"):
            link = " " + search.jump_link(
                guild_id, d["channel_id"], d["source_message_id"])
        dec_lines.append(f"- {d['decision'][:100]}{link}")
    term_lines = [f"- {t['term']}: {(t['description'] or '')[:80]}"
                  for t in material["terms"]]
    ctx = search.build_context(material["messages"], str(guild_id)) \
        if material["messages"] else "（なし）"
    return (
        f"社内Wikiページ「{topic}」を編纂して。"
        "Discordにピン留めされる「今の正解」のまとめで、会話が流れても"
        "ここを見れば現状が分かる、が目的。\n\n"
        "【決定事項（一次資料・リンク付き）】\n"
        + ("\n".join(dec_lines) or "（なし）")
        + "\n\n【関連する固有名詞】\n" + ("\n".join(term_lines) or "（なし）")
        + f"\n\n【関連する過去ログ】\n{ctx}\n\n"
        "ルール:\n"
        f"- 全体を{MAX_PAGE_CHARS}文字以内。見出し＋箇条書き中心\n"
        "- 決定事項に基づく確定情報を主役にする。日付・金額・担当は"
        "記録にある通りに書く（発明しない）\n"
        "- 確定でないものは「検討中」と明示するか書かない\n"
        "- 決定にリンクがあるものは行末にそのリンクを付ける（出典）\n"
        f"- 冒頭は「📖 **{topic}**」、末尾に「-# {today} 更新・"
        "自動編纂（内容が違ったら教えてほしいっス）」\n"
        "- ページ本文のみを出力（前置き不要）"
    )


def compile_page(db_path, topic, guild_id, *, model, invoke_fn=None,
                 today=None):
    """ページ本文を編纂。材料ゼロなら None。"""
    today = today or reminders.now_jst().strftime("%Y-%m-%d")
    material = gather(db_path, topic)
    if not (material["decisions"] or material["messages"]):
        return None
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=COMPILE_TIMEOUT_SEC).text)
    page = (fn(build_compile_prompt(topic, material, guild_id, today))
            or "").strip()
    return page[:MAX_PAGE_CHARS + 100] if page else None


def save_page(db_path, *, topic, channel_id, message_id, created_by):
    with db.connect(db_path) as conn:
        db.upsert_wiki_page(
            conn, topic=topic, channel_id=channel_id, message_id=message_id,
            last_decision_id=db.max_decision_id(conn),
            created_by=str(created_by),
            now=reminders.fmt(reminders.now_jst()))


def pages_needing_update(db_path):
    """新しい決定がトピックに触れているページだけ返す（決定論の先読み）。
    Returns: [(page, 現在のmax_decision_id)]"""
    out = []
    with db.connect(db_path) as conn:
        pages = db.wiki_pages_all(conn)
        if not pages:
            return []
        max_id = db.max_decision_id(conn)
        for p in pages:
            if max_id <= (p["last_decision_id"] or 0):
                continue
            news = db.decisions_after(conn, p["last_decision_id"] or 0,
                                      limit=20)
            words = topic_terms(p["topic"])
            hit = any(any(w in (d["decision"] or "") or
                          w in (d["topic"] or "") for w in words)
                      for d in news)
            if hit:
                out.append((p, max_id))
            else:
                # 触れていない: チェック位置だけ進める（次回の走査を軽くする）
                db.touch_wiki_page(conn, p["id"], max_id,
                                   reminders.fmt(reminders.now_jst()))
    return out


def mark_updated(db_path, page_id, max_decision_id):
    with db.connect(db_path) as conn:
        db.touch_wiki_page(conn, page_id, max_decision_id,
                           reminders.fmt(reminders.now_jst()))


WIKI_SKILL_NOTE = (
    "【Wikiページスキル】利用者が「〜のwikiを作って」「〜のまとめページを"
    "作って/更新して」と明確に頼んだときだけ、返信本文の最後に改行して"
    "[WIKI: トピック名] を出力すること（本文では作る旨を一言添える）。\n"
    "- トピック名は固有名詞で短く（例: [WIKI: サマーカップ]）\n"
    "- ページは決定事項・過去ログから自動編纂され、以後関連する決定が"
    "入るたびに自動で更新され続ける\n"
    "- 雑談・単なる質問には絶対に付けないこと"
)
