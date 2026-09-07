"""書き込みツール（Step C）。marker_actions の権限判定と -# 書式をそのまま移設する。

- 権限（管理者・本人・上限）は全部ここ（コード）で判定。LLM の申告は信用しない
- evidence（-# 行）は honesty.py の SUCCESS_DEEDS / FAIL_DEEDS と同じ書式にする。
  bot 側が本文に付けるので、既存の「できたフリ」検出がそのまま効く
- 失敗は ok:false で返す（モデルには失敗と見える）。例外は registry が握る
- 外部連携（シート等）の書き込みは integrations 側の責務。ここには置かない"""

import re

from core import db
from core import glossary
from core import reminders
from core import rules
from core.archive_tools.registry import Tool, register

_TO_SPLIT_RE = re.compile(r"[,、/・]+")
_CHANNEL_ID_RE = re.compile(r"^<#(\d+)>$")
_USERNAME_SPLIT_RE = re.compile(r"[\s/／,、]+")


def _now():
    return reminders.now_jst()


def _stamp():
    return reminders.fmt(_now())


def _ok(message, evidence, **extra):
    return {"ok": True, "message": message, "evidence": evidence, **extra}


def _ng(error, evidence):
    return {"ok": False, "error": error, "evidence": evidence}


def _actor_name(conn, ctx):
    row = conn.execute("SELECT display_name, name FROM users WHERE id=?",
                       (int(ctx.actor_id),)).fetchone()
    if row and (row[0] or row[1]):
        return row[0] or row[1]
    return f"user{ctx.actor_id}"


# ---------------------------------------------------------------- facts

def save_fact(ctx, args):
    topic = str(args.get("topic") or "").strip()[:40]
    fact = str(args.get("fact") or "").strip()[:300]
    if not topic or not fact:
        return _ng("topic と fact は必須",
                   "-# ⚠️ 事実を記録できなかった: topic/fact が空")
    with db.connect(ctx.db_path) as conn:
        fid, superseded = db.add_fact(
            conn, agent_id=ctx.agent_id, topic=topic, fact=fact,
            source_kind="conversation", source_message_id=ctx.message_id,
            channel_id=ctx.channel_id, stated_by=_actor_name(conn, ctx),
            created_at=_stamp())
    extra = f"・古い認識{superseded}件を上書き" if superseded else ""
    return _ok(f"事実を記録した（id={fid}）{extra}",
               f"-# 🧠 事実を記録(id={fid}): [{topic}] {fact[:60]}{extra}",
               id=fid)


register(Tool(
    name="save_fact", label="事実の記録",
    description=(
        "人が教えてくれた「いまの状況」を事実台帳に記録する（担当・状態・予定の"
        "変更・訂正など）。同じ topic の古い事実は自動で上書きされる。"
        "追跡タスクの期日/担当/状態・リマインダーの日時は専用ツールで直し、"
        "ここには書かない。「覚えておきます」と言うなら必ず呼ぶ。"),
    input_schema={"type": "object", "properties": {
        "topic": {"type": "string", "description": "主題（40字まで・上書きの鍵）"},
        "fact": {"type": "string", "description": "事実（300字まで）"}},
        "required": ["topic", "fact"]},
    kind="write", handler=save_fact))


def cancel_fact(ctx, args):
    fid = int(args.get("id") or 0)
    with db.connect(ctx.db_path) as conn:
        done = db.cancel_fact(conn, fid)
    if not done:
        return _ng(f"id={fid} は見つからないか既に取り消し済み",
                   f"-# ⚠️ 事実id={fid} は見つからないか既に取り消し済みです")
    return _ok(f"事実 id={fid} を取り消した",
               f"-# 🗑 事実を取り消し(id={fid})", id=fid)


register(Tool(
    name="cancel_fact", label="事実の取り消し",
    description="事実台帳の1件を取り消す（id は get_facts で確認）。",
    input_schema={"type": "object", "properties": {
        "id": {"type": "integer"}}, "required": ["id"]},
    kind="write", handler=cancel_fact))


# ---------------------------------------------------------------- rules

def save_rule(ctx, args):
    scope = str(args.get("scope") or "channel").lower()
    text = str(args.get("text") or "").strip()
    duration = args.get("duration")
    if scope not in rules.SCOPES:
        return _ng("scope は global/channel/user", "-# ⚠️ ルール登録に失敗: scope不正")
    if not text:
        return _ng("text は必須", "-# ⚠️ ルール登録に失敗: 本文が空")
    if len(text) > rules.MAX_RULE_LEN:
        return _ng(f"ルールが長すぎる（{rules.MAX_RULE_LEN}字以内）",
                   "-# ⚠️ ルール登録に失敗: 長すぎる")
    if scope == "global" and not ctx.is_admin:
        return _ng("全体共通ルールは管理者だけが設定できる",
                   "-# ⚠️ 全体共通ルールは管理者だけが設定できます"
                   "（このチャンネル/あなた向けなら設定可）")
    if duration and rules.parse_duration(str(duration)) is None:
        return _ng("duration は 7d / 2w / 1m のような形式",
                   "-# ⚠️ ルール登録に失敗: 期限の形式")
    key = rules.scope_key(scope, channel_id=ctx.channel_id,
                          user_id=ctx.actor_id)
    expires_at = rules.expiry_from(duration, _now()) if duration else None
    with db.connect(ctx.db_path) as conn:
        rid = db.add_rule(conn, agent_id=ctx.agent_id, scope=key,
                          rule_text=text, created_by=ctx.actor_id,
                          source_msg_id=ctx.message_id, created_at=_stamp(),
                          expires_at=expires_at)
    exp = rules.expiry_label(expires_at)
    return _ok(f"ルールを登録した（id={rid}, {rules.scope_label(key)}）{exp}",
               f"-# 📌 ルール登録(id={rid}, {rules.scope_label(key)}){exp}: "
               f"{text[:60]}", id=rid)


register(Tool(
    name="save_rule", label="ルール登録",
    description=(
        "今後の振る舞いのルールを保存する（「今後は〜して」「しばらく〜しないで」）。"
        "scope: channel=このチャンネル / user=この人向け / global=全体（管理者のみ）。"
        "duration は 7d / 2w / 1m 等（省略で恒久）。一時的な状況は save_fact へ。"),
    input_schema={"type": "object", "properties": {
        "text": {"type": "string"},
        "scope": {"type": "string", "enum": ["global", "channel", "user"]},
        "duration": {"type": "string", "description": "例: 7d, 2w, 1m"}},
        "required": ["text"]},
    kind="write", handler=save_rule))


def cancel_rule(ctx, args):
    rid = int(args.get("id") or 0)
    with db.connect(ctx.db_path) as conn:
        r = db.get_rule(conn, rid, ctx.agent_id)
        if r is None:
            return _ng(f"id={rid} のルールは見つからない",
                       f"-# ⚠️ id={rid} のルールは見つかりません")
        if not ctx.is_admin and r["created_by"] != ctx.actor_id:
            return _ng("他の人/共有のルールは管理者しか削除できない",
                       f"-# ⚠️ id={rid} は他の人/共有のルールなので"
                       "削除できません（管理者に相談を）")
        db.deactivate_rule(conn, rid, ctx.agent_id)
    return _ok(f"ルール id={rid} を削除した",
               f"-# 🗑 ルール削除(id={rid}): {r['rule_text'][:50]}", id=rid)


register(Tool(
    name="cancel_rule", label="ルール削除",
    description="保存済みルールを1件無効化する（自分が作ったもの。他人のは管理者のみ）。",
    input_schema={"type": "object", "properties": {
        "id": {"type": "integer"}}, "required": ["id"]},
    kind="write", handler=cancel_rule))


def request_capability(ctx, args):
    desc = str(args.get("description") or "").strip()[:200]
    if not desc:
        return _ng("description は必須", "-# ⚠️ 起票できませんでした")
    with db.connect(ctx.db_path) as conn:
        cid = db.add_capability_request(
            conn, agent_id=ctx.agent_id, description=desc,
            context=(ctx.question or "")[:500], requested_by=ctx.actor_id,
            source_msg_id=ctx.message_id, created_at=_stamp())
    return _ok(f"能力追加を起票した（id={cid}）",
               f"-# 🧩 能力追加を起票(id={cid}): {desc[:60]}", id=cid)


register(Tool(
    name="request_capability", label="能力の起票",
    description=(
        "自分に無い能力を求められたときに起票する（開発担当が拾う）。"
        "できないことを引き受けたフリをせず、正直に「未対応」と伝えた上で呼ぶ。"),
    input_schema={"type": "object", "properties": {
        "description": {"type": "string", "description": "何ができると良いか（200字まで）"}},
        "required": ["description"]},
    kind="write", handler=request_capability))


# ---------------------------------------------------------------- reminders

def _resolve_people(conn, names):
    """宛先名 → (mention, label, unresolved)。固有名詞辞書（Discord ID: username）→
    users テーブル（display_name / name の一意一致）の順。同名複数は解決しない。"""
    terms = db.terms_all(conn)
    mentions, labels, unresolved = [], [], []
    for name in names:
        uid = None
        for t in terms:
            if (t.get("term") or "").strip() != name:
                continue
            m = glossary.TERM_DISCORD_ID_RE.search(t.get("description") or "")
            if not m:
                continue
            username = _USERNAME_SPLIT_RE.split(m.group(1))[0]
            row = conn.execute("SELECT id FROM users WHERE name=?",
                               (username,)).fetchall()
            if len(row) == 1:
                uid = row[0][0]
            break
        if uid is None:
            rows = conn.execute(
                """SELECT id FROM users WHERE (display_name=? OR name=?)
                   AND COALESCE(is_bot,0)=0""", (name, name)).fetchall()
            if len(rows) == 1:
                uid = rows[0][0]
        if uid is None:
            unresolved.append(name)
        elif f"<@{uid}>" not in mentions:
            mentions.append(f"<@{uid}>")
            labels.append(f"@{name}")
    return (" ".join(mentions) or None), (" ".join(labels) or None), unresolved


def _resolve_channel(conn, ctx, token):
    """#名前 / <#id> → (channel_id, label, note)。発言者が投稿したことのある ch だけ
    （見えない ch への代理投稿を防ぐ近似。guild を持たないサーバ側の判定）。"""
    token = (token or "").strip()
    m = _CHANNEL_ID_RE.match(token)
    if m:
        rows = conn.execute("SELECT id, name FROM channels WHERE id=?",
                            (int(m.group(1)),)).fetchall()
    else:
        rows = conn.execute("SELECT id, name FROM channels WHERE name=?",
                            (token.lstrip("#"),)).fetchall()
    if len(rows) != 1:
        return None, None, (f"-# ⚠️ チャンネル「{token}」が見つからないため、"
                            "このチャンネルに通知します")
    cid, name = rows[0]
    posted = conn.execute(
        "SELECT 1 FROM messages WHERE channel_id=? AND author_id=? LIMIT 1",
        (cid, int(ctx.actor_id))).fetchone()
    if not posted and not ctx.is_admin:
        return None, None, ("-# ⚠️ あなたが書き込めないチャンネルには設定"
                            "できません。このチャンネルに通知します")
    return cid, f"#{name}", None


def add_reminder(ctx, args):
    content = str(args.get("content") or "").strip()
    if not content:
        return _ng("content は必須", "-# ⚠️ 登録できなかった: 内容が空")
    try:
        due = reminders.parse_dt(str(args.get("due") or ""))
    except ValueError as e:
        return _ng(str(e), f"-# ⚠️ 登録できなかった: {e}")
    repeat = str(args.get("repeat") or "once")
    if repeat not in reminders.REPEATS:
        return _ng(f"repeat は {'/'.join(reminders.REPEATS)}",
                   "-# ⚠️ 登録できなかった: repeat が不正")
    notes = []
    deliver = ctx.channel_id
    channel_label = mention = label = None
    with db.connect(ctx.db_path) as conn:
        if args.get("channel"):
            cid, channel_label, note = _resolve_channel(conn, ctx,
                                                        args["channel"])
            if cid:
                deliver = cid
            if note:
                notes.append(note)
        to_raw = str(args.get("to") or "")
        names = [n.strip().lstrip("@") for n in _TO_SPLIT_RE.split(to_raw)
                 if n.strip().lstrip("@")]
        if any(n.lower() in ("everyone", "全員", "全体") for n in names):
            notes.append("-# ⚠️ 全員/ロール宛はメンション権限を持つ人だけ"
                         "設定できます。依頼者宛にしました")
            names = []
        if names:
            mention, label, unresolved = _resolve_people(conn, names)
            if mention is None:
                notes.append(f"-# ⚠️ 宛先「{to_raw}」が見つからないか"
                             "同名が複数いるため依頼者宛にしました")
            elif unresolved:
                notes.append("-# ⚠️ 宛先のうち「" + "、".join(unresolved)
                             + f"」は見つかりませんでした（{label} には届きます）")
        user_name = _actor_name(conn, ctx)
    entry, err = reminders.add_reminder(
        channel_id=deliver, user_id=ctx.actor_id, user_name=user_name,
        content=content, due=due, repeat=repeat,
        max_active=ctx.reminder_max_active, agent_id=ctx.agent_id,
        mention=mention, mention_label=label, channel_label=channel_label)
    if entry is None:
        notes.append(f"-# ⚠️ 登録できなかった: {err}")
        return _ng(err, "\n".join(notes))
    line = reminders.format_entry_line(entry)
    notes.append("-# 登録: " + line)
    return _ok(f"リマインダーを登録した: {line}", "\n".join(notes),
               id=entry["id"])


register(Tool(
    name="add_reminder", label="リマインダー登録",
    description=(
        "リマインダーを登録する。due は 'YYYY-MM-DD HH:MM'（相対表現は自分で"
        "絶対日時に直す。現在時刻は文脈にある）。repeat: once/daily/weekly/"
        "monthly/monthly_end。to は人名（カンマ区切り）、channel は '#名前' で"
        "通知先チャンネル。省略時は依頼者宛・このチャンネル。"
        "「リマインドしておく」と言うなら必ず呼び、結果の登録行を確認する。"),
    input_schema={"type": "object", "properties": {
        "content": {"type": "string"},
        "due": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
        "repeat": {"type": "string",
                   "enum": list(reminders.REPEATS)},
        "to": {"type": "string", "description": "宛先の人名（任意）"},
        "channel": {"type": "string", "description": "#チャンネル名（任意）"}},
        "required": ["content", "due"]},
    kind="write", handler=add_reminder, skill="reminder"))


def cancel_reminder(ctx, args):
    rid = int(args.get("id") or 0)
    entry, reason = reminders.cancel_reminder(rid, ctx.actor_id,
                                              is_admin=ctx.is_admin)
    if entry:
        owner = ""
        if entry["user_id"] != str(ctx.actor_id):
            owner = f"（{entry['user_name']}さんの分・管理者権限で取消）"
        return _ok(f"リマインダー id={rid} を取り消した",
                   f"-# キャンセル: id={rid} {entry['content'][:40]}{owner}",
                   id=rid)
    if reason == "not_found":
        return _ng(f"id={rid} は存在しない", f"-# ⚠️ id={rid} は存在しません")
    if reason == "ended":
        old = reminders.find_entry(rid)
        lab = {"done": "配信済み", "cancelled": "取消済み"}.get(
            (old or {}).get("status"), "終了済み")
        return _ng(f"id={rid} は既に{lab}", f"-# ⚠️ id={rid} は既に{lab}です")
    old = reminders.find_entry(rid)
    owner = (old or {}).get("user_name") or "他の人"
    return _ng("本人か管理者しか取り消せない",
               f"-# ⚠️ id={rid} は{owner}さんのリマインダーなので、"
               "本人か管理者しか取り消せません")


register(Tool(
    name="cancel_reminder", label="リマインダー取消",
    description="リマインダーを取り消す（id は list_reminders で確認。本人か管理者のみ）。",
    input_schema={"type": "object", "properties": {
        "id": {"type": "integer"}}, "required": ["id"]},
    kind="write", handler=cancel_reminder, skill="reminder"))


# ---------------------------------------------------------------- tasks

def update_task(ctx, args):
    from core import tasks
    key = args.get("key") or args.get("id")
    action = str(args.get("action") or "").lower()
    due = str(args.get("due") or "").strip() or None
    ok, notes = tasks.update(
        ctx.db_path, ctx.agent_id, key, action, due=due,
        actor_id=str(ctx.actor_id), is_admin=ctx.is_admin)
    if ok:
        parsed = tasks.parse_key(key)
        if parsed and parsed[0] == "action":
            with db.connect(ctx.db_path) as conn:
                db.add_proactive_log(
                    conn, agent_id=ctx.agent_id, kind="deadline",
                    action={"done": "done", "cancel": "cancel", "due": "due",
                            "open": "reopen"}[action],
                    channel_id=ctx.channel_id,
                    trigger_message_id=ctx.message_id,
                    detail=str(key), created_at=_stamp())
        return _ok("追跡タスクを更新した", "\n".join(notes), key=str(key))
    return _ng(notes[0][3:] if notes else "更新できなかった",
               "\n".join(notes) or "-# ⚠️ 納期追跡: 更新できませんでした")


register(Tool(
    name="update_task", label="追跡タスクの更新",
    description=(
        "追跡タスクを 完了(done)・取消(cancel)・期日変更(due)・再開(open) する。"
        "key は list_tasks の A6 / H27 / R57 の形（A=議事録TODO・H=宿題・"
        "R=リマインダー）。担当の人か管理者のみ。"
        "「金曜にリスケ」は action=due, due=YYYY-MM-DD。手放した A を戻すのは open。"
        "事実台帳には書かずこちらで直す。"),
    input_schema={"type": "object", "properties": {
        "key": {"type": "string", "description": "A6 / H27 / R57"},
        "action": {"type": "string", "enum": ["done", "cancel", "due", "open"]},
        "due": {"type": "string", "description": "action=due のとき YYYY-MM-DD"}},
        "required": ["key", "action"]},
    kind="write", handler=update_task, skill="action_tracking"))


# ---------------------------------------------------------------- glossary

def save_glossary(ctx, args):
    wrong = str(args.get("wrong") or "").strip()[:40]
    correct = str(args.get("correct") or "").strip()[:40]
    if not wrong or not correct or wrong == correct:
        return _ng("wrong と correct（異なる文字列）は必須",
                   "-# ⚠️ 単語帳に登録できませんでした")
    fixed = glossary.save(ctx.db_path, wrong, correct, str(ctx.actor_id))
    note = f"-# 📖 単語帳に登録: 「{wrong}」→「{correct}」（今後の議事録・回答・検索に反映"
    if fixed:
        note += f"・台帳{fixed}件も修正"
    note += "）"
    return _ok(f"単語帳に登録した（既存{fixed}件修正）", note)


register(Tool(
    name="save_glossary", label="単語帳の登録",
    description=("誤表記→正表記の対応を登録する（音声認識の当て字など）。"
                 "以後の議事録・回答・検索で自動置換される。"),
    input_schema={"type": "object", "properties": {
        "wrong": {"type": "string"}, "correct": {"type": "string"}},
        "required": ["wrong", "correct"]},
    kind="write", handler=save_glossary))


def save_term(ctx, args):
    term = str(args.get("term") or "").strip()[:40]
    desc = str(args.get("description") or "").strip()[:200]
    if not term:
        return _ng("term は必須", "-# ⚠️ 固有名詞を登録できませんでした")
    glossary.save_term(ctx.db_path, term, desc, str(ctx.actor_id))
    d = f"（{desc}）" if desc else ""
    return _ok(f"固有名詞「{term}」を登録した",
               f"-# 📛 固有名詞を登録: 「{term}」{d}"
               "（議事録・回答で正式表記として扱います）")


register(Tool(
    name="save_term", label="固有名詞の登録",
    description=("人名・チーム名などの正式表記を辞書に登録する。人の場合は "
                 "description に「Discord ID: ユーザー名」を含めると宛先解決に使える。"),
    input_schema={"type": "object", "properties": {
        "term": {"type": "string"}, "description": {"type": "string"}},
        "required": ["term"]},
    kind="write", handler=save_term))


# ---------------------------------------------------------------- lessons

def save_lesson(ctx, args):
    text = str(args.get("text") or "").strip()[:200]
    polarity = str(args.get("polarity") or "advice")
    if not text:
        return _ng("text は必須", "-# ⚠️ 教訓を記録できませんでした")
    if polarity not in ("advice", "up", "down"):
        polarity = "advice"
    with db.connect(ctx.db_path) as conn:
        added = db.add_proactive_lesson(
            conn, agent_id=ctx.agent_id, kind="conversation",
            channel_id=ctx.channel_id, message_id=ctx.message_id,
            text=text, created_at=_stamp(), polarity=polarity)
    if not added:
        return _ng("この投稿からの教訓は既に記録済み",
                   "-# ⚠️ 教訓は既に記録済みです")
    return _ok("教訓を記録した", f"-# 📝 教訓を記録: {text[:60]}")


register(Tool(
    name="save_lesson", label="教訓の記録",
    description=("自分の振る舞いへの指摘や、うまくいった/いかなかった経験を"
                 "教訓として残す（次回 recall_lessons で思い出す）。"
                 "polarity: advice=助言 / up=良かった例 / down=失敗例。"),
    input_schema={"type": "object", "properties": {
        "text": {"type": "string"},
        "polarity": {"type": "string", "enum": ["advice", "up", "down"]}},
        "required": ["text"]},
    kind="write", handler=save_lesson))


# ---------------------------------------------------------------- 自発発言の枠

def set_proactive_quota(ctx, args):
    from core import proactive
    if not ctx.is_admin:
        return _ng("自発発言の枠は管理者だけが変更できる",
                   "-# ⚠️ 自発発言の枠は管理者だけが変更できます")
    target = str(args.get("agent_id") or "")
    try:
        quota = int(args.get("quota"))
    except (TypeError, ValueError):
        quota = -1
    if target not in ctx.agent_ids or not 0 <= quota <= proactive.QUOTA_MAX:
        return _ng("枠の指定が不正", f"-# ⚠️ 枠の指定が不正です（{target} {quota}）")
    with db.connect(ctx.db_path) as conn:
        db.set_proactive_quota(conn, target, quota, _stamp())
    return _ok(f"{target} の自発発言枠を {quota}回/日 に変更した",
               f"-# ⚙️ {target}の自発発言枠を{quota}回/日に変更しました"
               "（次の観察周期から）")


register(Tool(
    name="set_proactive_quota", label="自発発言枠の変更",
    description=("同僚AIの自発発言の枠（1日の回数上限）を変える（管理者のみ）。"
                 "agent_id は設定にあるエージェントid。"),
    input_schema={"type": "object", "properties": {
        "agent_id": {"type": "string"}, "quota": {"type": "integer"}},
        "required": ["agent_id", "quota"]},
    kind="write", handler=set_proactive_quota, skill="quota"))
