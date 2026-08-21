#!/usr/bin/env python3
"""事実台帳（2026-08-18）。

決定台帳（RM#4）が「**決めたこと**」を持つのに対し、事実台帳は
「**いま実際どうなっているか**」を持つ。人間の訂正・状況説明の受け皿。

背景（実事故）: グッズ納期の件でかばちゃんが「8/8販売は完了済み、9月到着分は
オンライン向け在庫」と説明し、戦子は「認識更新するっス」と答えたが、
DBには何も残らなかった。原因は指針（rules）・決定（decisions）・語（terms）の
どれでもない「事実の現状」に受け皿が無かったこと。書く場所が無いので
誠実に更新しようとしても口約束で終わる構造だった。

方式は既存のマーカー方式と同型（LLMが出力 → コードが副作用実行）:
    [FACT: 主題 | 事実の内容]
    [FACT_CANCEL: id]
同じ主題の古い事実は自動で superseded になる（最新だけがactive＝
「認識が更新される」をデータで表現する）。

単体テスト: ./venv/bin/python -m unittest test_facts -v
"""

import re

FACT_MARKER_RE = re.compile(r"\[FACT:\s*([^\]]+)\]")
FACT_CANCEL_RE = re.compile(r"\[FACT_CANCEL:\s*(\d+)\s*\]")

MAX_TOPIC_LEN = 40
MAX_FACT_LEN = 200
MAX_PER_ANSWER = 3
SEARCH_LIMIT = 8


def parse_fact(payload):
    """'主題 | 事実' をパース。不正は ValueError。"""
    parts = [p.strip() for p in (payload or "").split("|")]
    if len(parts) < 2:
        raise ValueError("FACT は「主題 | 事実の内容」の形で書く")
    topic = parts[0]
    fact = "|".join(parts[1:]).strip()
    if not topic or not fact:
        raise ValueError("主題と事実の両方が必要")
    if len(topic) > MAX_TOPIC_LEN:
        raise ValueError(f"主題が長すぎる（{MAX_TOPIC_LEN}字以内）")
    if len(fact) > MAX_FACT_LEN:
        raise ValueError(f"事実が長すぎる（{MAX_FACT_LEN}字以内）")
    return {"topic": topic, "fact": fact}


def extract_markers(answer):
    """回答から FACT / FACT_CANCEL を全て除去し
    (本文, 追加[], キャンセルid[], エラー文[]) を返す。
    マーカーはパース成否に関わらず必ず除去する（生マーカーを晒さない）。"""
    text = answer or ""
    adds, errors = [], []
    for m in FACT_MARKER_RE.finditer(text):
        try:
            adds.append(parse_fact(m.group(1)))
        except ValueError as e:
            errors.append(str(e))
    cancel_ids = [int(x) for x in FACT_CANCEL_RE.findall(text)]
    text = FACT_CANCEL_RE.sub("", FACT_MARKER_RE.sub("", text))
    if len(adds) > MAX_PER_ANSWER:
        errors.append(f"事実の記録は1回の回答で{MAX_PER_ANSWER}件まで"
                      f"（{len(adds) - MAX_PER_ANSWER}件は保存しない）")
        adds = adds[:MAX_PER_ANSWER]
    return text.strip(), adds, cancel_ids, errors


def build_ledger_block(db_path, keywords, guild_id):
    """回答Contextに載せる【事実台帳】（該当なしなら空文字）。
    決定台帳と同じく出典リンクを添える＝引用すれば出典ゲートを通る。
    補助情報なので、DBが未初期化・読めない等の場合は空で退避する
    （台帳の不在で回答そのものを落とさない）。"""
    import sqlite3

    from core import db
    from core import search
    try:
        with db.connect(db_path) as conn:
            rows = db.search_facts(conn, keywords, limit=SEARCH_LIMIT)
    except sqlite3.Error as e:
        print(f"事実台帳を読めなかったため省略: {e}")
        return ""
    if not rows:
        return ""
    lines = []
    for r in rows:
        link = ""
        if r.get("source_message_id") and r.get("channel_id"):
            link = " " + search.jump_link(guild_id, r["channel_id"],
                                          r["source_message_id"])
        who = f"（{r['stated_by']}さん談）" if r.get("stated_by") else ""
        lines.append(f"- [{r['topic']}] {r['fact']}{who}{link}")
    return ("【事実台帳（人が教えてくれた現状。決定台帳より新しい情報として"
            "扱い、矛盾する古い記録より優先する）】\n" + "\n".join(lines))


def build_skill_note(recent_facts=None):
    """事実記録スキルの指示文。訂正の受け皿として使わせる。"""
    listing = ""
    if recent_facts:
        rows = "\n".join(f"  id={f['id']} [{f['topic']}] {f['fact'][:60]}"
                         for f in recent_facts[:10])
        listing = f"現在記録されている事実:\n{rows}\n"
    return (
        "【事実の記録スキル】利用者が**状況や事実を教えてくれた・訂正した**とき"
        "（例:「その件はもう完了してる」「AではなくBになった」「この予定は"
        "こう変わった」）は、返信本文の最後に改行して次の形式で記録すること:\n"
        "[FACT: 主題 | 事実の内容]\n"
        f"- 主題は短い固有名詞（{MAX_TOPIC_LEN}字以内）。同じ主題の古い事実は"
        "自動で上書きされる（＝認識が更新される）\n"
        "- 事実は1〜2文で簡潔に。教えてもらった内容だけを書き、推測を混ぜない\n"
        "- 例: [FACT: 8/8グッズ販売 | 販売は完了済み。9月到着分はオンライン"
        "ストアと今後のオフラインイベントで販売予定]\n"
        "取り消しを頼まれたら: [FACT_CANCEL: id]\n"
        f"{listing}"
        "使い分け: **今後の振る舞い**の指示は [RULE:]、**決まったこと**は"
        "台帳が自動記録、**言葉の表記**は [TERM:]、"
        "**いまどうなっているか**はこの [FACT:]。\n"
        "重要: 単なる雑談・質問・進行中の相談には付けないこと"
        "（迷ったら保存しない側に倒す）。"
    )
