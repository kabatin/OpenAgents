#!/usr/bin/env python3
"""失敗と間違いの台帳。

❌は「何もしない」で終わらせず、どこが違ったかを一言もらって貯める。自動では
やり切れなかったこと（決定の波及チェックの「自動では直せない」など）・頼まれたけど
できなかったこと・想定外のツールの失敗・できたフリの検出も同じ台帳に入れる。
同じ種類がたまったら起票し、開発BOTの改善へ回す材料にする。

- 理由を聞くのは1つの提案につき1回だけ・答えは任意（しつこくしない）
- 理由は「聞いた投稿」への返信でも、「❌した提案」への返信でも受け取る
- 書くのは実際にやること（貯める）だけ。「覚える」とは書かない
- 点検で赤（乗っ取り訓練の突破・回答品質の急落）は1件で即起票する

単体テスト: core/test_misses.py
"""

from core import db
from core import reminders

SOURCE_LABEL = {
    "ripple": "決定の波及チェックの案に❌",
    "ripple_manual": "波及チェックで自動では直せなかった",
    "capability": "頼まれたけどできなかった（能力リクエスト）",
    "tool_failed": "ツールの実行に失敗した",
    "fake_done": "できたフリを検出した",
    "taught": "人に直し方を教わって直した",
    "drill_breach": "乗っ取り訓練で突破された",
    "quality_drop": "回答品質のチェックで点数が急に下がった",
}
# 点検で赤（安全・品質の定期点検が落ちた）＝1件で即起票。開発BOTへの起票の目印
RED_MARK = "[点検で赤]"
RED_TOPIC_PREFIXES = ("security:", "quality:")
QUALITY_DROP = 0.4          # 直近の中央値からこれ以上下がったら急落
QUALITY_MIN_HISTORY = 3
RED_STEPS = {
    "security": (
        "やること: (1) 本番と同じ条件で再現する（core/injection_drill.py の"
        " build_attack と同じ組み立て・回答と同じ設定のモデル）。(2) どの枠から入った"
        "命令に従ったかを特定し、防御文（core/search.py の INJECTION_GUARD など）か"
        "組み立てを直す。(3) 直した作業場で `python -m core.verify_safety` を流し、"
        "全項目が耐えたことを確かめる（反映前にシステムも同じ点検を流して要約に貼る）。"
        "防御を弱める変更・ほかの項目を悪化させる変更はしない。"),
    "quality": (
        "やること: (1) 下がった問題の回答と採点理由を state/golden_eval の直近2回で"
        "見比べ、共通する原因（プロンプト・ツール・検索の変更）を特定する。(2) 原因を"
        "直し、根拠を要約に書く。原因が揺れ（モデルの気まぐれ）なら、直さずにそう書く。"),
}
REJECTION_SOURCES = ("ripple",)          # 理由つきの❌だけを数える
NEVER_FILE = ("capability", "taught")    # 起票済み・見本そのもの＝起票の単位にしない
THRESHOLD = 3
# 想定内の失敗（設定でオフ・上限・入力不足・存在しないツール）は手がかりにならない
_EXPECTED_ERRORS = ("設定でオフ", "unknown tool", "上限", "必須", "見つからな",
                    "権限", "管理者のみ", "本人か管理者")


def _now():
    return reminders.fmt(reminders.now_jst())


def question(done_text):
    """❌への返事。やったこと＋理由のお願い（任意）。"""
    return (f"-# {done_text}。どこが違ったか、この投稿に返信で一言もらえると"
            "改善のヒントとして貯めます（任意・聞くのはこの1回だけです）")


def record_rejection(db_path, *, agent_id, source, ref_message_id, context,
                     topic=None):
    """❌された提案を記録。同じ提案で2回目なら None（聞くのは1回だけ）。"""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO misses(agent_id, source, topic, context,
                   ref_message_id, created_at) VALUES(?,?,?,?,?,?)""",
            (agent_id, source, topic or source, (context or "")[:300],
             ref_message_id, _now()))
        return cur.lastrowid if cur.rowcount == 1 else None


def set_ask_message(db_path, miss_id, message_id):
    with db.connect(db_path) as conn:
        conn.execute("UPDATE misses SET ask_message_id=? WHERE id=?",
                     (message_id, miss_id))


def record_gap(db_path, *, agent_id, source, context, detail, topic=None):
    """自動ではやり切れなかったことを記録（理由は聞かない）。"""
    with db.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO misses(agent_id, source, topic, context, detail,
                   created_at) VALUES(?,?,?,?,?,?)""",
            (agent_id, source, topic or source, (context or "")[:300],
             (detail or "")[:500], _now()))


def worth_recording_tool_error(error):
    """ツールの失敗のうち、改善の手がかりになるものか（想定内の失敗は除く）。"""
    return not any(h in str(error or "") for h in _EXPECTED_ERRORS)


def save_reason(db_path, replied_to_id, text, user_id):
    """理由の返信を受け取る。対象（未回答の❌）でなければ False。"""
    text = (text or "").strip()
    if not text or replied_to_id is None:
        return False
    with db.connect(db_path) as conn:
        row = conn.execute(
            """SELECT id FROM misses WHERE status='open' AND reason IS NULL
                 AND (ask_message_id=? OR ref_message_id=?)
               ORDER BY id DESC LIMIT 1""",
            (replied_to_id, replied_to_id)).fetchone()
        if row is None:
            return False
        conn.execute(
            """UPDATE misses SET reason=?, reason_by=?, status='answered',
                   answered_at=? WHERE id=?""",
            (text[:500], str(user_id), _now(), row[0]))
    return True


def recent(db_path, limit=100):
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, agent_id, source, context, detail, reason, status,
                      ask_message_id, created_at, answered_at
               FROM misses ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    keys = ("id", "agent_id", "source", "context", "detail", "reason",
            "status", "ask_message_id", "created_at", "answered_at")
    return [dict(zip(keys, r)) for r in rows]


def _is_red(topic):
    return str(topic or "").startswith(RED_TOPIC_PREFIXES)


def _describe(topic, rows):
    source = rows[0]["source"]
    label = SOURCE_LABEL.get(source, source)
    if _is_red(topic):
        kind = topic.split(":", 1)[0]
        lines = [f"{RED_MARK} {label}（種類: {topic}）。", RED_STEPS.get(kind, "")]
    else:
        lines = [f"[失敗の常連] {label}が{len(rows)}回たまった（種類: {topic}）。"
                 "同じ失敗が起きないよう、作り方を直す。"]
    for r in rows[:5]:
        piece = r["context"] or ""
        if r["detail"]:
            piece += f"／{r['detail']}"
        if r["reason"]:
            who = "人の直し方" if r["source"] == "ripple_manual" else "人の理由"
            piece += f"／{who}: {r['reason']}"
        lines.append(f"- {piece[:160]}")
    return "\n".join(lines)


def quality_drop(mean, history):
    """回答品質チェックの急落か（純粋関数）。急落なら説明文、揺れの範囲なら None。
    history は直近の平均点（新しい順・今回を含まない）。"""
    if mean is None or len(history) < QUALITY_MIN_HISTORY:
        return None
    window = sorted(history[:4])
    mid = len(window) // 2
    median = window[mid] if len(window) % 2 else (window[mid - 1] + window[mid]) / 2
    if median - mean < QUALITY_DROP:
        return None
    return f"平均{mean:.2f}（直近の中央値{median:.2f}から{median - mean:.2f}下落）"


def file_repeated(db_path, threshold=THRESHOLD):
    """同じ種類がたまった失敗を、開発BOTへの起票にする。起票した id のリスト。
    ❌は理由つきのものだけ数える。使った記録には印を付け、二度起票しない。"""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, agent_id, source, topic, context, detail, reason
               FROM misses WHERE filed_cap_id IS NULL ORDER BY id""").fetchall()
    keys = ("id", "agent_id", "source", "topic", "context", "detail", "reason")
    groups = {}
    for r in (dict(zip(keys, x)) for x in rows):
        if r["source"] in NEVER_FILE:
            continue
        if r["source"] in REJECTION_SOURCES and not r["reason"]:
            continue
        groups.setdefault(r["topic"] or r["source"], []).append(r)
    filed = []
    for topic, items in groups.items():
        if len(items) < (1 if _is_red(topic) else threshold):
            continue
        with db.connect(db_path) as conn:
            cap_id = db.add_capability_request(
                conn, agent_id=items[0]["agent_id"] or "",
                description=_describe(topic, items),
                context=f"失敗と間違いの台帳から自動起票（misses: {topic}）",
                requested_by="misses", source_msg_id=None, created_at=_now())
            conn.executemany("UPDATE misses SET filed_cap_id=? WHERE id=?",
                             [(cap_id, r["id"]) for r in items])
        filed.append(cap_id)
    return filed


# ---------------------------------------------- 正解の見本（人に教わって直したとき)

_REPEAT = {"once": "1回", "daily": "毎日", "weekly": "毎週", "monthly": "毎月",
           "monthly_end": "毎月末"}


def _hm(stamp):
    return str(stamp or "").replace("T", " ")[:16]


def snapshot(db_path):
    """直す前後を比べるための、リマインダーと追跡タスクの今の状態。"""
    rems = {r["id"]: (r["due"], r["repeat"], r["content"][:40], r["status"])
            for r in reminders.list_active()}
    with db.connect(db_path) as conn:
        tasks = {r[0]: (r[1], r[2], (r[3] or "")[:40]) for r in conn.execute(
            "SELECT id, due_date, status, task FROM action_items"
            " WHERE status IN ('open','stale')")}
    return {"reminders": rems, "tasks": tasks}


def diff_snapshots(before, after):
    """前後の違いを人が読める行に（純粋関数）。"""
    lines = []
    b, a = before["reminders"], after["reminders"]
    for rid in sorted(set(a) - set(b)):
        due, rep, text, _ = a[rid]
        lines.append(f"リマインダー#{rid} を登録（{_hm(due)}・{_REPEAT.get(rep, rep)}「{text}」）")
    for rid in sorted(set(b) - set(a)):
        lines.append(f"リマインダー#{rid} を止めた（「{b[rid][2]}」）")
    for rid in sorted(set(a) & set(b)):
        if a[rid][0] != b[rid][0]:
            lines.append(f"リマインダー#{rid} の次回 {_hm(b[rid][0])} → {_hm(a[rid][0])}")
        if a[rid][2] != b[rid][2]:
            lines.append(f"リマインダー#{rid} の文面を「{a[rid][2]}」に")
    bt, at = before["tasks"], after["tasks"]
    for tid in sorted(set(at) & set(bt)):
        if at[tid][0] != bt[tid][0]:
            lines.append(f"タスクA{tid}「{at[tid][2]}」の期日 {bt[tid][0]} → {at[tid][0]}")
    for tid in sorted(set(bt) - set(at)):
        lines.append(f"タスクA{tid}「{bt[tid][2]}」を閉じた")
    return lines


def record_teaching(db_path, *, agent_id, context, instruction, changes):
    """人の指示で直した見本を残し、同じ場面の「直せなかった」を答え済みにする。
    見本（指示・変化）は、のちの起票で開発BOTがテストの正解にする材料。"""
    now = _now()
    with db.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO misses(agent_id, source, topic, context, detail, reason,
                   status, created_at, answered_at)
               VALUES(?, 'taught', 'taught', ?, ?, ?, 'answered', ?, ?)""",
            (agent_id, (context or "")[:300], "\n".join(changes)[:1000],
             (instruction or "")[:500], now, now))
        conn.execute(
            """UPDATE misses SET reason=?, status='answered', answered_at=?
               WHERE source='ripple_manual' AND context=? AND status='open'""",
            ((instruction or "")[:500], now, context))
