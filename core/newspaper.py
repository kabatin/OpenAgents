#!/usr/bin/env python3
"""社内新聞の編集長（進化ロードマップ#93）。

週刊「社内新聞」を **1枚のビジュアル画像＋短い要約テキスト** で発行する
（テキストだけの週報は読まれないため。管理者指示 2026-08-01）。

  1) 材料集め: 決定・完了・イベント・盛り上がった話題（決定論）
  2) 見出し化: LLMが3〜5本の見出しへ（短く・具体的に）
  3) 画像生成: 画像生成スキル（integrations の連携）へ新聞レイアウトを依頼
  4) 投稿: 画像＋要約テキスト（画像生成が失敗したらテキストだけで発行）

単体テスト: ./venv/bin/python -m unittest test_meta_pack -v
"""

import json
import os
import re
from datetime import timedelta

from core import invoke_claude
from core import db
from core import reminders
STATE_PREFIX = "newspaper:"
WEEKDAY_DEFAULT = 4     # 金曜
HOUR_DEFAULT = 20       # 週の締め
MIN_MATERIAL = 3
MAX_HEADLINES = 5
TIMEOUT_SEC = 180
_JSON_RE = re.compile(r"\{.*\}", re.S)


def should_publish(db_path, agent_id, *, weekday=WEEKDAY_DEFAULT,
                   hour=HOUR_DEFAULT, now=None):
    now = now or reminders.now_jst()
    if now.weekday() != weekday or now.hour != hour:
        return False
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, STATE_PREFIX + agent_id)
    if state and state.get("last_run_at"):
        try:
            last = reminders.parse_dt(state["last_run_at"])
            if (now - last).days < 3:
                return False
        except ValueError:
            pass
    return True


def mark_published(db_path, agent_id, now=None):
    now = now or reminders.now_jst()
    with db.connect(db_path) as conn:
        db.set_proactive_state(conn, STATE_PREFIX + agent_id,
                               last_checked_message_id=0,
                               last_run_at=reminders.fmt(now))


def collect_material(db_path, now=None):
    """今週の出来事（決定・完了・イベント確定）。"""
    now = now or reminders.now_jst()
    since = reminders.fmt(now - timedelta(days=7))
    out = []
    with db.connect(db_path) as conn:
        for kind, sql in (
                ("決定", "SELECT decision FROM decisions WHERE created_at >= ?"),
                ("完了", """SELECT task FROM action_items
                            WHERE status='done' AND created_at >= ?"""),
                ("予定", """SELECT name || '（' || event_date || '）'
                            FROM events WHERE status='planned'
                              AND created_at >= ?""")):
            for (t,) in conn.execute(sql, (since,)).fetchall():
                if (t or "").strip():
                    out.append(f"[{kind}] {t.strip()[:100]}")
    return out


def build_headline_prompt(material, week_label):
    listing = "\n".join(f"- {m}" for m in material[:40])
    return (
        f"社内週刊新聞（{week_label}号）の紙面を作って。"
        "「読みたくなる読み物」が目的（見出しの羅列にしない）。\n\n"
        f"【今週の記録】\n{listing}\n\n"
        "ルール:\n"
        f"- 記事は3〜{MAX_HEADLINES}本。1本目がトップ記事（今週最大のニュース）\n"
        "- headline: 新聞見出しらしく短く（20字以内）\n"
        "- deck: 見出しを補う小見出し（15字前後。金額・日付など具体を入れる）\n"
        "- body: 本文2文（120字以内）。事実に基づく（記録に無いことは"
        "書かない）。同じ内容の重複記録は1本にまとめる\n"
        "- art: 記事に添える挿し絵の指定を1行で"
        "（例: タオルとペンライトのイラスト）。人物の顔は指定しない\n"
        "- lead は紙面全体を1〜2文でまとめた編集後記風の要約\n"
        '- 出力はJSONのみ: {"lead": "…", "articles": [{"headline": "…", '
        '"deck": "…", "body": "…", "art": "…"}]}'
    )


def parse_headlines(raw):
    """紙面JSONの検証つき解釈。旧形式（headlines配列）にも耐える。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    articles = []
    for a in (data.get("articles") or [])[:MAX_HEADLINES]:
        if not isinstance(a, dict):
            continue
        headline = str(a.get("headline") or "").strip()[:40]
        if not headline:
            continue
        articles.append({
            "headline": headline,
            "deck": str(a.get("deck") or "").strip()[:30],
            "body": str(a.get("body") or "").strip()[:160],
            "art": str(a.get("art") or "").strip()[:60]})
    if not articles:   # 旧形式フォールバック
        articles = [{"headline": str(h).strip()[:40], "deck": "",
                     "body": "", "art": ""}
                    for h in (data.get("headlines") or [])
                    if str(h).strip()][:MAX_HEADLINES]
    if not articles:
        return None
    return {"lead": str(data.get("lead") or "").strip()[:200],
            "articles": articles,
            "headlines": [a["headline"] for a in articles]}


def edit(db_path, week_label, *, model, invoke_fn=None, now=None):
    """見出し生成。材料不足なら None。"""
    material = collect_material(db_path, now)
    if len(material) < MIN_MATERIAL:
        return None
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=TIMEOUT_SEC).text)
    return parse_headlines(fn(build_headline_prompt(material, week_label)))


def build_image_prompt(paper, week_label, issue_no=None):
    """画像生成AIへの依頼文（純粋関数）。タブロイド紙スタイル
    （2026-08-09サンプル②を管理者が採用）。"""
    articles = paper.get("articles") or [
        {"headline": h, "deck": "", "body": "", "art": ""}
        for h in paper["headlines"]]
    blocks = []
    for i, a in enumerate(articles):
        label = "【トップ記事】" if i == 0 else f"【記事{i + 1}】"
        blocks.append(
            f"{label}\n見出し: {a['headline']}\n"
            + (f"デッキ: {a['deck']}\n" if a['deck'] else "")
            + (f"本文: {a['body']}\n" if a['body'] else "")
            + (f"挿し絵: {a['art']}" if a['art'] else
               "挿し絵: 記事に合うシンプルなイラスト"))
    issue = f"第{issue_no}号" if issue_no else ""
    return (
        "以下の記事で、日本語の社内向け週刊新聞「社内新聞」の第一面を"
        f"1枚の画像にしてください。日付は{week_label} {issue}。\n\n"
        + "\n\n".join(blocks) +
        "\n\nスタイル: 元気なタブロイド紙。題字「社内新聞」は"
        "深緑(#0e7a68)の帯に白抜きで上部に、日付・号数もその帯に入れる。"
        "トップ記事は紙面の半分近くを使い大きな挿し絵つき、見出しは太い"
        "ゴシック体で大きく。残りの記事は下段に整理して並べ、それぞれ"
        "小さめの挿し絵を添える。挿し絵はカラーのフラットイラストで、"
        "チームらしい活気を出す。\n"
        "共通の指示:\n"
        "- 文字は全て横書き。日本語は誤字なく正確に描く（これが最重要）\n"
        "- 各記事の見出し・デッキ・本文を全て文字として入れる\n"
        "- 挿し絵は文字と干渉しない位置に配置する"
    )


def build_caption(paper, week_label, issue_no=None):
    """テキスト側の紙面（記事化）。画像が「一面の顔」、こちらが「記事面」。"""
    issue = f" 第{issue_no}号" if issue_no else ""
    lines = [f"🗞 **社内新聞 {week_label}号{issue}**"]
    if paper.get("lead"):
        lines.append(f"> {paper['lead']}")
    articles = paper.get("articles") or [
        {"headline": h, "deck": "", "body": ""}
        for h in paper["headlines"]]
    for a in articles:
        head = f"**■ {a['headline']}**"
        if a.get("deck"):
            head += f" — {a['deck']}"
        lines.append(head)
        if a.get("body"):
            lines.append(a["body"])
    return "\n".join(lines)
