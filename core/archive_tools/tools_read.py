"""読み取りツール（Step B）。既存モジュールを呼ぶ薄い層。
description には「いつ使うか・書き忘れると何が起きるか」を書く
（使える値の一覧だけでは足りない）。"""

from core import db
from core import glossary
from core import reminders
from core import search
from core.archive_tools.registry import RESULT_NOTE, Tool, register


def _kw(args, key="keywords", limit=8):
    raw = args.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(k).strip() for k in raw if str(k).strip()][:limit]


def _link(ctx, r):
    if r.get("source_message_id") and r.get("channel_id"):
        return " " + search.jump_link(ctx.guild_id, r["channel_id"],
                                      r["source_message_id"])
    return ""


# ---------------------------------------------------------------- search

def search_messages(ctx, args):
    """社内ログの全文検索（trigram FTS）。いま応答中の ch は除外する
    （直近会話は既に文脈にあるため）。"""
    keywords = _kw(args)
    if not keywords:
        return {"ok": False, "error": "keywords を1つ以上指定する"}
    limit = max(1, min(int(args.get("limit") or 12), 24))
    rows = search.search_messages(ctx.db_path, keywords, limit=limit,
                                  exclude_channel_id=ctx.channel_id)
    if not rows:
        return {"ok": True, "hits": 0,
                "message": "社内ログに該当なし" + RESULT_NOTE}
    return {"ok": True, "hits": len(rows),
            "message": search.build_context(rows, ctx.guild_id) + "\n"
            + RESULT_NOTE}


register(Tool(
    name="search_messages",
    description=(
        "社内チャットの過去ログを全文検索する。社内の事柄（誰が・いつ・何を"
        "決めた/言った）を答える前に必ず使い、根拠の投稿リンクを回答に添える。"
        "1回で見つからなければ同義語・別表記で言い換えて再検索してよい。"
        "keywords は名詞中心で3文字以上を推奨。"),
    input_schema={"type": "object", "properties": {
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "検索語（1〜8個）"},
        "limit": {"type": "integer", "description": "最大件数（既定12・上限24）"}},
        "required": ["keywords"]},
    kind="read", handler=search_messages))


# ---------------------------------------------------------------- ledgers

def get_facts(ctx, args):
    """事実台帳（人が教えてくれた現状）。keywords 空なら直近。"""
    with db.connect(ctx.db_path) as conn:
        rows = db.search_facts(conn, _kw(args), limit=10)
    if not rows:
        return {"ok": True, "hits": 0, "message": "事実台帳に該当なし"}
    lines = []
    for r in rows:
        who = f"（{r['stated_by']}さん談）" if r.get("stated_by") else ""
        lines.append(f"- id={r['id']} [{r['topic']}] {r['fact']}{who}"
                     f"{_link(ctx, r)}")
    return {"ok": True, "hits": len(rows),
            "message": "【事実台帳（決定台帳より新しい情報として優先）】\n"
            + "\n".join(lines) + "\n" + RESULT_NOTE}


register(Tool(
    name="get_facts",
    description=(
        "事実台帳を引く。人が会話で教えてくれた「いまの状況」（担当・状態・"
        "予定の変更など）が主題ごとに1件ずつ入っている。社内の現状を答える"
        "前に確認する。keywords 空なら直近10件。"),
    input_schema={"type": "object", "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}}}},
    kind="read", handler=get_facts))


def get_decisions(ctx, args):
    with db.connect(ctx.db_path) as conn:
        rows = db.search_decisions(conn, _kw(args), limit=10)
    if not rows:
        return {"ok": True, "hits": 0, "message": "決定台帳に該当なし"}
    lines = []
    for r in rows:
        topic = f"[{r['topic']}] " if r.get("topic") else ""
        date = f"（{r['decided_on']}決定）" if r.get("decided_on") else ""
        lines.append(f"- id={r['id']} {topic}{r['decision']}{date}"
                     f"{_link(ctx, r)}")
    return {"ok": True, "hits": len(rows),
            "message": "【決定事項台帳（過去に確定した事項）】\n"
            + "\n".join(lines) + "\n" + RESULT_NOTE}


register(Tool(
    name="get_decisions",
    description=(
        "決定事項台帳を引く。定例議事録や会話で確定した決定が出典リンク付きで"
        "入っている。「〜って決まってたっけ？」「前と話が違わない？」に答える"
        "前に確認する。事実台帳の方が新しければそちらを優先する。"),
    input_schema={"type": "object", "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}}}},
    kind="read", handler=get_decisions))


# ---------------------------------------------------------------- lists

def list_reminders(ctx, args):
    rows = reminders.list_active(None if ctx.is_admin else ctx.actor_id)
    if not rows:
        return {"ok": True, "hits": 0, "message": "有効なリマインダーなし"}
    lines = [f"- {reminders.format_entry_line(r)}"
             + (f"（{r.get('user_name')}さん）" if ctx.is_admin else "")
             for r in rows]
    return {"ok": True, "hits": len(rows),
            "message": "【リマインダー】\n" + "\n".join(lines)}


register(Tool(
    name="list_reminders",
    description=("発言者のリマインダー一覧（管理者は全員分）。取消や確認の"
                 "依頼に答える前に id を確かめる。"),
    input_schema={"type": "object", "properties": {}},
    kind="read", handler=list_reminders, skill="reminder"))


def list_tasks(ctx, args):
    from core import tasks
    rows = tasks.list_all(ctx.db_path, ctx.agent_id, actor_id=ctx.actor_id,
                          is_admin=ctx.is_admin, guild_id=ctx.guild_id)
    kind = str(args.get("kind") or "").strip()
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if not rows:
        return {"ok": True, "hits": 0, "message": "追跡中のタスクなし"}
    return {"ok": True, "hits": len(rows),
            "message": "【追跡中のタスク（A=議事録TODO / H=宿題 / R=リマインダー）】\n"
            + "\n".join(tasks.format_line(r) for r in rows)
            + "\n" + RESULT_NOTE}


register(Tool(
    name="list_tasks",
    description=("追跡中のタスク一覧を3種まとめて返す（A=議事録TODO・H=宿題・"
                 "R=リマインダー。key・期日・担当・状態）。"
                 "完了・取消・期日変更の依頼に答える前に key を確かめる。"
                 "kind で絞れる（action/homework/reminder）。"),
    input_schema={"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["action", "homework", "reminder"]}}},
    kind="read", handler=list_tasks, skill="action_tracking"))


def lookup_terms(ctx, args):
    words = _kw(args, "words", limit=10)
    terms = glossary.load_terms(ctx.db_path)
    if words:
        low = [w.lower() for w in words]
        terms = [t for t in terms
                 if any(w in (t.get("term") or "").lower()
                        or w in (t.get("description") or "").lower()
                        for w in low)]
    if not terms:
        return {"ok": True, "hits": 0, "message": "固有名詞辞書に該当なし"}
    lines = [f"- {t['term']}" + (f": {t['description']}"
                                 if t.get("description") else "")
             for t in terms[:30]]
    return {"ok": True, "hits": len(terms),
            "message": "【固有名詞辞書（正式表記）】\n" + "\n".join(lines)}


register(Tool(
    name="lookup_terms",
    description=("社内の固有名詞辞書（人名・チーム名・正式表記）。名前の"
                 "表記に迷ったら引く。words 空なら全件（上限30）。"),
    input_schema={"type": "object", "properties": {
        "words": {"type": "array", "items": {"type": "string"}}}},
    kind="read", handler=lookup_terms))


def recall_lessons(ctx, args):
    labels = {"advice": "自己改善メモ", "up": "うまくいった例",
              "down": "失敗例"}
    lines = []
    with db.connect(ctx.db_path) as conn:
        for pol in ("advice", "up", "down"):
            for r in db.recent_proactive_lessons(conn, ctx.agent_id,
                                                 limit=5, polarity=pol):
                lines.append(f"- [{labels[pol]}] {r['text']}")
    if not lines:
        return {"ok": True, "hits": 0, "message": "教訓なし"}
    return {"ok": True, "hits": len(lines),
            "message": "【自分の教訓】\n" + "\n".join(lines)}


register(Tool(
    name="recall_lessons",
    description=("自分の過去の教訓（自己採点の蒸留・👍👎の実例）を思い出す。"
                 "判断に迷う依頼や、以前失敗したかもしれない場面で引く。"),
    input_schema={"type": "object", "properties": {}},
    kind="read", handler=recall_lessons))
