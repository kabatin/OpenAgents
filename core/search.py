#!/usr/bin/env python3
"""
検索＋回答ロジック（Phase 3）。

意味ベクトルは後付け（Phase 2b）とし、まずは追加依存ゼロで動く構成：
  1. 質問 → claude CLI でキーワード抽出（query expansion）
  2. trigram全文検索で関連メッセージを取得（3文字未満はLIKEでフォールバック）
  3. 取得結果を根拠に claude CLI が引用付きで回答

生成・キーワード抽出はどちらも claude CLI（APIキー不要・追加費用なし）。
"""

import os
import re
import json
import subprocess
import sqlite3

from core import chat
from core import config
from core import db
from core import llm
from core import msgref
from core import paths
from core.attachments import (
    DEFAULT_QUESTION as ATTACH_DEFAULT_QUESTION,
    TIMEOUT_SEC as ATTACH_TIMEOUT_SEC,
)

#: 設定を渡されなかったときのモデル（利用者が config.llm.model を書けばそちら）
DEFAULT_MODEL = llm.BUILTIN_PROVIDERS["claude"]["default_model"]
CLAUDE_TIMEOUT_SEC = llm.LONG_TIMEOUT_SEC

# 添付読解でツールを許可する際のガード（プロンプトインジェクション対策）。
# 主防御は --permission-mode default: ヘッドレスではcwd（添付一時dir）外の
# Readが自動拒否され、Readをcwdに閉じ込められる。このdenyは万一モード指定が
# 効かない場合の二重防御（~/.ssh・token入りconfig等の最重要秘密を遮断。
# denyはモード/allowより常に優先）。
_TOOL_GUARD_SETTINGS = json.dumps(
    {"permissions": {"deny": ["Read(~/**)"]}})

# エージェント定義: {"name": 表示名, "persona_files": [パス...], "role": 担当説明}
# persona_files は上から順に連結される（人物像 → 話し方 → 守ること の順を想定）。
# 既定は同梱テンプレート（personas/README.md 参照）。ダッシュボードで
# 性格を選ぶと personas/<エージェントID>.md が作られ、そちらが使われる。
DEFAULT_PERSONA_FILES = [
    os.path.join(paths.PERSONAS_DIR, "assistant.template.md"),
]
DEFAULT_AGENT = {
    "name": "エージェント",
    "persona_files": DEFAULT_PERSONA_FILES,
    "role": "",
}


def load_persona(persona_files):
    """ペルソナファイル群を読み込んで連結。読めないものはスキップ。"""
    parts = []
    for path in persona_files:
        try:
            with open(path, encoding="utf-8") as f:
                parts.append(f.read().strip())
        except OSError:
            continue
    return "\n\n".join(parts) + "\n\n" if parts else ""


_CACHED_CONFIG = None


def _config():
    """設定を1回だけ読んで使い回す（1プロセス内で何度も読まない）。

    テストや呼び出し側が明示的に cfg を渡せば、そちらが優先される。
    """
    global _CACHED_CONFIG
    if _CACHED_CONFIG is None:
        try:
            _CACHED_CONFIG = config.load()
        except config.ConfigError:
            _CACHED_CONFIG = {}
    return _CACHED_CONFIG


def run_claude(prompt, model=None, timeout=CLAUDE_TIMEOUT_SEC,
               allowed_tools=None, cwd=None, cfg=None):
    """プロンプトを渡して本文を受け取る。

    テキスト生成だけなら設定で選ばれた任意のAI（Claude Code / Codex CLI /
    自前のCLI）を使う。**ツールを要求されたときだけ Claude Code 固定**で、
    それ以外のプロバイダを選んでいる場合は理由を添えて断る
    （黙ってツール無しで実行すると、添付を読まずに答えてしまう）。

    allowed_tools: 有効化するツール名タプル（例: ("Read",)。添付読解用）。
    指定時はホーム配下Read禁止のガード設定を併せて渡す。
    cwd: 作業ディレクトリ（添付の一時dirを想定）。
    """
    config = cfg if cfg is not None else _config()
    if not allowed_tools:
        return llm.generate(prompt, config, timeout=timeout)

    # --- ここから下はツールが要る経路（Claude Code 専用） ---
    if not llm.supports_tools(config):
        raise RuntimeError(llm.describe_limits(config))
    spec = llm.spec_for(config, "claude")
    claude_bin = llm.find_binary(spec)
    if claude_bin is None:
        raise RuntimeError("claude CLI が見つかりません")
    argv = [claude_bin, "-p", "--model", model or llm.model_for(config),
            "--tools", ",".join(allowed_tools),
            "--permission-mode", "default",   # cwd外Readを自動拒否
            "--settings", _TOOL_GUARD_SETTINGS]
    proc = subprocess.run(
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI 失敗 (exit={proc.returncode}): {proc.stderr.strip()[:500]}"
        )
    reply = proc.stdout.strip()
    if not reply:
        raise RuntimeError("claude の出力が空でした")
    return reply


def extract_keywords(question, model=DEFAULT_MODEL, history="", claude_fn=None,
                     syn_note=""):
    """質問から検索キーワードを抽出。失敗時は質問自体を分割して返す。

    claude_fn: 生成呼び出しの差し替え口（prompt -> text）。
    未指定なら従来どおり run_claude を使う（runner経路は invoke_claude を渡す）。
    """
    ctx = f"【直近の会話（文脈参照用）】\n{history}\n\n" if history else ""
    prompt = (
        "次の質問から、社内チャットを全文検索するためのキーワードを抽出してください。\n"
        "・名詞や固有名詞を中心に\n"
        "・直近の会話がある場合、指示語（それ/その件/この表 等）が指す対象を補って展開する\n"
        "・重要: 同義語・関連語・表記ゆれも必ず展開する"
        "（例: 引っ越し→移住,転居 / 締切→納期,期限 / 報酬→ギャラ,料金）\n"
        + syn_note +
        "・各キーワードはできれば3文字以上、全体で6〜12個\n"
        "・JSON配列のみ出力（説明文・コードブロック不要）\n\n"
        f"{ctx}質問: {question}"
    )
    fn = claude_fn or (lambda p: run_claude(p, model=model, timeout=120))
    try:
        raw = fn(prompt)
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            kws = json.loads(m.group(0))
            kws = [str(k).strip() for k in kws if str(k).strip()]
            if kws:
                return kws
    except Exception:
        pass
    # フォールバック：日本語/英数の連なりを素朴に抽出
    return [w for w in re.split(r"[\s、。,.!?！？]+", question) if len(w) >= 2][:6]


def _fts_search(conn, keyword, limit):
    """1キーワードのFTS検索。3文字未満はLIKEでフォールバック。"""
    if len(keyword) >= 3:
        q = '"' + keyword.replace('"', '""') + '"'
        try:
            return conn.execute(
                """SELECT m.id, bm25(messages_fts) AS rank
                   FROM messages_fts
                   JOIN messages m ON m.id = messages_fts.rowid
                   WHERE messages_fts MATCH ? AND m.deleted = 0
                   ORDER BY rank LIMIT ?""",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass
    # LIKEフォールバック（短いキーワードや特殊文字）
    return conn.execute(
        """SELECT id, 0.0 AS rank FROM messages
           WHERE deleted = 0 AND content LIKE ? LIMIT ?""",
        (f"%{keyword}%", limit),
    ).fetchall()


def search_messages(db_path, keywords, limit=24, per_keyword=12,
                    exclude_channel_id=None, recent_from_id=None):
    """複数キーワードで検索し、ヒット回数とbm25でスコアリングして上位を返す。

    exclude_channel_id: 応答中のチャンネル。ここの投稿は【直近の会話】として
        別途プロンプトに載るため、検索結果と二重に並べない。
    recent_from_id: そのプロンプトに載っている最古のメッセージID。指定すると
        **除外はここから新しいぶんだけ**になり、同じchの古いログは検索対象に残る。
        省略すると ch 全体を除外する（＝古い挙動）。

    省略時にch全体を落とす旧仕様は、1チャンネルで使っていると
    アーカイブの全件が消えて検索が一度もヒットしない。呼び出し側は
    recent_from_id を渡すこと。
    """
    scores = {}
    # sqlite3.connect の with はトランザクション管理であって**接続を閉じない**。
    # mac/Linux では GC が隠すが、Windows では開いたハンドルがファイル削除を
    # 塞ぐ（実際にCIで発覚）。必ず閉じる db.connect を使う
    with db.connect(db_path) as conn:
        for kw in keywords:
            for mid, rank in _fts_search(conn, kw, per_keyword):
                # ヒット数を加点、bm25(負の値ほど良い)を反映
                s = scores.get(mid, {"hits": 0, "rank": 0.0})
                s["hits"] += 1
                s["rank"] += float(rank)
                scores[mid] = s

        # チャンネル名がキーワードに一致する場合、そのchの最近メッセージを候補に追加
        for kw in keywords:
            if len(kw) < 2:
                continue
            for (mid,) in conn.execute(
                """SELECT m.id FROM messages m JOIN channels c ON c.id=m.channel_id
                   WHERE c.name LIKE ? AND m.deleted=0
                   ORDER BY m.id DESC LIMIT ?""",
                (f"%{kw}%", per_keyword),
            ).fetchall():
                s = scores.get(mid, {"hits": 0, "rank": 0.0})
                s["hits"] += 1
                scores[mid] = s

        if not scores:
            return []

        ranked = sorted(
            scores.items(), key=lambda kv: (-kv[1]["hits"], kv[1]["rank"])
        )[:limit]
        ids = [mid for mid, _ in ranked]

        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT m.id, m.channel_id, c.name, u.display_name, m.content,
                       m.created_at,
                       (SELECT COUNT(*) FROM attachments a
                        WHERE a.message_id=m.id AND a.is_image=1) AS imgs,
                       (SELECT COUNT(*) FROM attachments a
                        WHERE a.message_id=m.id AND a.is_video=1) AS vids,
                       (SELECT COUNT(*) FROM attachments a
                        WHERE a.message_id=m.id) AS atts
                FROM messages m
                LEFT JOIN channels c ON c.id=m.channel_id
                LEFT JOIN users u ON u.id=m.author_id
                WHERE m.id IN ({placeholders})
                  AND NOT (? IS NOT NULL AND m.channel_id = ?
                           AND (? IS NULL OR m.id >= ?))""",
            ids + [exclude_channel_id, exclude_channel_id,
                   recent_from_id, recent_from_id],
        ).fetchall()

    by_id = {r[0]: r for r in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    return [
        {
            "id": r[0], "channel_id": r[1], "channel": r[2] or "?",
            "author": r[3] or "?", "content": r[4] or "",
            "created_at": r[5], "imgs": r[6], "vids": r[7], "atts": r[8],
        }
        for r in ordered
    ]


def jump_link(guild_id, channel_id, message_id):
    """発言へのリンク。形式はプラットフォーム実装が登録する（core/chat.py）。"""
    return chat.link_to(msgref.PLATFORM, guild_id, channel_id, message_id)


def build_context(rows, guild_id):
    lines = []
    for i, r in enumerate(rows, 1):
        media = []
        if r["imgs"]:
            media.append(f"画像{r['imgs']}")
        if r["vids"]:
            media.append(f"動画{r['vids']}")
        if r["atts"] and not media:
            media.append(f"添付{r['atts']}")
        media_s = f" | 添付:{'/'.join(media)}" if media else ""
        link = jump_link(guild_id, r["channel_id"], r["id"])
        date = (r["created_at"] or "")[:10]
        lines.append(
            f"[{i}] (#{r['channel']}, {r['author']}, {date}){media_s}\n"
            f"    {r['content']}\n    link: {link}"
        )
    return "\n".join(lines)


def build_history(history, per_msg=1000):
    """直近やりとり [{author, content, is_bot}]（古い順）を会話ログ文字列に整形。"""
    if not history:
        return ""
    lines = []
    for h in history:
        text = (h.get("content") or "").strip()
        if not text:
            continue
        if len(text) > per_msg:
            text = text[:per_msg] + "…"
        who = h.get("author") or "?"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


ANSWER_SYSTEM_TMPL = """あなたはチームのチャットの全会話を把握するアシスタント「{name}」です。{role_block}
親しみやすく簡潔な日本語で、質問や相談に答えてください。

【直近の会話】は今この質問chで交わされている直前のやりとりです（あなた自身の
発言も含む）。まずこれを読み、指示語（それ/その表/さっきの件 等）が何を指すかを
理解したうえで、会話の流れに沿って自然に答えてください。

【関連メッセージ】は社内ログから自動検索した参考情報です。次の方針で使い分けます:
- 質問が社内の事柄で、関連メッセージに答えがある → それを根拠に答え、末尾に
  「参照:」として根拠投稿のジャンプリンクを箇条書きで示す。添付(画像/動画)がある
  投稿が関連すれば「📎関連ファイル:」としてそのリンクも併記する（クリックで原本表示）。
- 関連メッセージに答えが無い、または一般的な質問・雑談・作業依頼 → 社内ログに
  こだわらず、{name}として普通に答える（無理に「見つかりませんでした」と言わない。参照も不要）。
- ただし社内固有の事実を、根拠なく創作しないこと。
- 【自信度の明示】社内固有の事実に確信が持てないまま答える部分があるときは断定を
  避け、回答の末尾に「-# 🤔 自信度低め: 〜の部分は要確認」を1行だけ添える
  （確信がある回答には付けない）。
- 【トーンの調整】相手の発言に苛立ち・焦り・疲れが見えるときは、冗長な前置きや
  絵文字を減らし、結論から短く答える。**感情そのものには言及しない**
  （「お疲れですか」「イライラしてます?」等は書かない。推定を外したときに
  相手を不快にさせるため、調整は自分の話し方だけに留める）。
- 全体を2000文字以内に収める。"""

GENERAL_SYSTEM_TMPL = """あなたはチームのチャットのアシスタント「{name}」です。{role_block}
親しみやすく簡潔な日本語で、質問・相談・雑談・作業依頼に答えてください。
【直近の会話】がある場合はまずそれを読み、指示語（それ/その件 等）が指す対象を
踏まえて会話の流れに沿って答えること。社内固有の事実を根拠なく断定しないこと。
確信が持てないまま答える部分があるときは断定を避け、回答末尾に
「-# 🤔 自信度低め: 〜の部分は要確認」を1行だけ添えること（確信があれば付けない）。"""


def _build_system(template, agent):
    """テンプレートの {name}/{role_block} を埋める。
    role は自己完結した文（複数行可: 担当説明・同僚一覧・スキル指示など）。"""
    role = (agent.get("role") or "").strip()
    role_block = f"\n{role}" if role else ""
    return template.format(name=agent["name"], role_block=role_block)


def answer_question(db_path, guild_id, question, model=DEFAULT_MODEL,
                    exclude_channel_id=None, history=None, agent=None,
                    attachments=None, references=None, extra_blocks=None,
                    recent_from_id=None):
    """質問→キーワード抽出→検索→回答生成 を一気通貫で行う。

    history: 質問chの直近やりとり [{author, content, is_bot}]（古い順）。
             指示語の解決と会話の流れの維持に使う。社内事実の検索根拠には使わない。
    agent:   {"name", "persona_files", "role"} のdict。None ならアーカイブ担当（DEFAULT_AGENT）。
    attachments: attachments.AttachmentContext。読み込み可能な添付があるときだけ
             claudeにReadツールを許可し、cwd=一時dir・延長タイムアウトで実行する。
    references: 利用者がリンク/IDで指定した投稿の整形済みブロック（msgref製、
             ヘッダ込み）。指定時はそのまま Context に注入する。
    extra_blocks: 外部連携が用意した現況スナップショットの一覧（各要素は
             ヘッダ込みの文字列。integrations.context_blocks 製）。
             この経路は --tools "" なので流出経路の追加対処は不要。
    """
    if agent is None:
        agent = DEFAULT_AGENT
    persona = load_persona(agent["persona_files"])
    convo = build_history(history)
    convo_block = f"【直近の会話】\n{convo}\n\n" if convo else ""
    ref_block = f"\n\n{references}" if references else ""
    for block in (extra_blocks or []):
        ref_block += f"\n\n{block}"
    att_block = ""
    claude_kwargs = {}
    if attachments is not None and attachments.block:
        att_block = f"\n\n{attachments.block}"
        if attachments.has_supported:
            claude_kwargs = {"allowed_tools": ("Read",),
                             "cwd": attachments.dir,
                             "timeout": ATTACH_TIMEOUT_SEC}
    question = (question or "").strip()
    if not question:
        # 無言添付（テキストなしのメンション/リプライ投稿）。
        # 検索キーワードが無いのでログ検索はスキップして添付の説明に徹する
        question = ATTACH_DEFAULT_QUESTION
        keywords, rows = [], []
    else:
        from core import glossary
        syn = glossary.synonyms_note(glossary.load_pairs(db_path))
        keywords = extract_keywords(question, model=model, history=convo,
                                    syn_note=syn)
        rows = search_messages(db_path, keywords,
                               exclude_channel_id=exclude_channel_id,
                               recent_from_id=recent_from_id)
    if not rows:
        # 社内ログにヒット無し → キャラとして普通に回答（雑談・一般質問・作業依頼）
        system = _build_system(GENERAL_SYSTEM_TMPL, agent)
        prompt = (f"{persona}{system}\n\n{convo_block}【質問】\n{question}"
                  f"{ref_block}{att_block}")
        answer = run_claude(prompt, model=model, **claude_kwargs)
        return {"answer": answer, "keywords": keywords, "hits": 0}
    context = build_context(rows, guild_id)
    system = _build_system(ANSWER_SYSTEM_TMPL, agent)
    prompt = (
        f"{persona}{system}\n\n{convo_block}【質問】\n{question}\n\n"
        f"【関連メッセージ】\n{context}{ref_block}{att_block}"
    )
    answer = run_claude(prompt, model=model, **claude_kwargs)
    return {"answer": answer, "keywords": keywords, "hits": len(rows)}
