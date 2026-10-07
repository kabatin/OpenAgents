#!/usr/bin/env python3
"""重い作業は裏で進めて、途中経過と結果をスレッドに置く。プラットフォーム非依存の部分。

資料づくりや調べものをその場でやり切ってから返事すると、最長10分黙り、時間切れも
起きる。回答モデルが「数分以上かかる作業」と判断したら返答の最後に
[TASK: 作業担当への具体的な指示] を書き、BOTは即答（取りかかります）→スレッドを開く→
裏で claude を起動→数分ごとに途中経過→完了したら依頼者に結果とファイルを渡す。
Discord 側は platforms/discord/bg_task_runner.py。

安全:
- 作業は使い捨ての作業フォルダの中だけ（Read はフォルダ外を自動拒否、Write/Edit は
  フォルダの絶対パスに限って許可）。Bash は渡さない
- 社内ログ等の道具は読む系だけ（書き込み・送信系は渡さない）
- 完了の報告はシステムが書く（モデルに「できた」と書かせない）
- Claude Code 専用（道具を使うため）。他のAIを選んでいるときは引き受けない

単体テスト: core/test_bg_tasks.py
"""

import os
import re

from core import db
from core import reminders

TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch")
MAX_PARALLEL = 2
TIMEOUT_SEC = 30 * 60
REPORT_INTERVAL_SEC = 180
MAX_BUDGET_USD = 3.0
MAX_FILES = 10

# 設定の範囲（ダッシュボードのカタログと揃える）
_LIMITS = {"max_parallel": (MAX_PARALLEL, 1, 5),
           "timeout_min": (TIMEOUT_SEC // 60, 5, 120),
           "report_interval_min": (REPORT_INTERVAL_SEC // 60, 1, 30)}

_MARKER = re.compile(r"\s*\[TASK:\s*(.*?)\]\s*$", re.S)
_LABEL = {"WebSearch": "Webで調べる", "WebFetch": "Webページを読む",
          "Read": "ファイルを読む", "Write": "ファイルを書く", "Edit": "ファイルを直す",
          "Glob": "ファイルを探す", "Grep": "ファイルを探す"}

SKILL_NOTE = (
    "【裏の作業】資料・表・文章の作成、いくつもの調べもの・比較、ファイルを作る作業のように"
    "数分以上かかりそうな依頼は、その場でやり切らない。返答は「取りかかります」程度に短くし、"
    "返答の最後に [TASK: 作業担当への具体的な指示] を1つだけ書く（依頼の意図・欲しい形・"
    "注意点を具体的に。聞き間違いや言葉足らずは補う）。システムが裏で作業し、スレッドで途中経過と"
    "結果を知らせる。すぐ答えられる質問・雑談には使わない。")

WORKER_PROMPT = """あなたは「{name}」の作業担当として、依頼された作業を最後までやり切ります。

【依頼】
{instruction}

【やり方】
- 作業フォルダ（今のフォルダ）の中だけで作業する。作った成果物（表・文章・資料など）は
  分かりやすい日本語のファイル名で作業フォルダに保存する（Markdown なら .md）。
- 社内の事柄は社内ログの道具で調べ、根拠になった投稿があれば成果物にリンクを残す。
  世の中の情報は Web で調べ、出典を残す。分からないことは作らずに「要確認」と書く。
- 最後に、依頼者への報告を {name} の口調で書く: 何を作ったか（ファイル名）、要点3〜5行、
  確認が要る点。1500字以内。「送信した」「登録した」など、していないことは書かない。
（現在: {now}）"""


def normalize(raw):
    """設定 skills.bg_tasks を解釈する。無効なら None（既定はオフ）。
    True だけの指定は既定値で有効。上限はカタログと同じ範囲に収める。"""
    if raw is True:
        raw = {"enabled": True}
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    out = {"enabled": True}
    for key, (default, lo, hi) in _LIMITS.items():
        try:
            out[key] = min(hi, max(lo, int(raw.get(key, default))))
        except (TypeError, ValueError):
            out[key] = default
    return out


def extract_marker(answer):
    """返答の最後の [TASK: 指示] を外す。(本文, 指示|None)。空の指示は捨てる。"""
    text = answer or ""
    m = _MARKER.search(text)
    if not m:
        return text, None
    instr = m.group(1).strip()
    return text[:m.start()].rstrip(), (instr or None)


def steps_from_event(ev):
    """stream-json の1イベントから、使った道具の名前を取り出す。"""
    if ev.get("type") != "assistant":
        return []
    return [b.get("name", "") for b in (ev.get("message") or {}).get("content") or []
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def _label(name):
    if name.startswith("mcp__"):
        return "社内ログを調べる"
    return _LABEL.get(name, "作業する")


def label_steps(names):
    """道具名を人の言葉に（同じことが続くときは1回にまとめる）。"""
    out = []
    for n in names:
        lab = _label(n)
        if not out or out[-1] != lab:
            out.append(lab)
    return out


def progress_text(elapsed_sec, steps):
    minutes = max(1, int(elapsed_sec // 60))
    if not steps:
        return f"⏳ {minutes}分経過: 考え中…"
    return f"⏳ {minutes}分経過: " + " → ".join(steps[-3:]) + " …"


def due_report(*, now, last_at, interval=REPORT_INTERVAL_SEC, changed):
    """途中経過を出す頃合いか（前回から interval 秒以上・何か進んだときだけ）。"""
    return changed and now - last_at >= interval


def rule_path(path):
    """許可ルールに書く絶対パス（Claude Code は POSIX 形式・先頭 // が絶対パスの印）。
    Windows の C:\\x は //c/x にする。"""
    p = str(path).replace("\\", "/").rstrip("/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        p = f"/{m.group(1).lower()}/{m.group(2)}"
    return "/" + p if p.startswith("/") else "//" + p


def allow_rules(workdir, mcp_allow=()):
    """事前承認する道具。書き込みは作業フォルダの中だけ。"""
    root = rule_path(os.path.realpath(workdir))
    return (f"Write({root}/**)", f"Edit({root}/**)", "WebSearch", "WebFetch",
            *mcp_allow)


def output_files(workdir, limit_bytes):
    """作業フォルダの成果物（添付できる大きさのもの）と、大きすぎて添付できないもの。"""
    files, skipped = [], []
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        if os.path.getsize(path) > limit_bytes or len(files) >= MAX_FILES:
            skipped.append(name)
        else:
            files.append(path)
    return files, skipped


def worker_prompt(name, instruction, now=None):
    return WORKER_PROMPT.format(name=name, instruction=instruction,
                                now=reminders.fmt_human(now or reminders.now_jst()))


# ------------------------------------------------------------ 台帳

_COLS = ("id", "agent_id", "channel_id", "thread_id", "requester_id",
         "instruction", "status", "summary", "created_at", "finished_at")


def _now():
    return reminders.fmt(reminders.now_jst())


def create(db_path, *, agent_id, channel_id, thread_id, requester_id, instruction):
    with db.connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO bg_tasks(agent_id, channel_id, thread_id, requester_id,
                   instruction, status, created_at) VALUES(?,?,?,?,?,'running',?)""",
            (agent_id, channel_id, thread_id, str(requester_id),
             (instruction or "")[:2000], _now()))
        return cur.lastrowid


def get(db_path, task_id):
    with db.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM bg_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(zip(_COLS, row)) if row else None


def running_count(db_path, agent_id):
    with db.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM bg_tasks WHERE agent_id=? AND"
                            " status='running'", (agent_id,)).fetchone()[0]


def finish(db_path, task_id, status, summary):
    with db.connect(db_path) as conn:
        conn.execute("UPDATE bg_tasks SET status=?, summary=?, finished_at=? WHERE id=?",
                     (status, (summary or "")[:2000], _now(), task_id))


def interrupt_running(db_path, agent_id):
    """再起動で途中になった作業を interrupted にして返す（スレッドに知らせるため）。"""
    with db.connect(db_path) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM bg_tasks WHERE agent_id=? AND status='running'", (agent_id,))]
        conn.executemany("UPDATE bg_tasks SET status='interrupted', finished_at=? WHERE id=?",
                         [(_now(), i) for i in ids])
    return [get(db_path, i) for i in ids]
