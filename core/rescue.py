#!/usr/bin/env python3
"""未回答質問の救済（進化ロードマップ#31）。

24時間以上誰も答えていない質問を検出し、アーカイブ担当が拾って答える。

頼まれていない投稿なので、出す根拠が弱いものは出さない（切り出し元の運用では、
過去の代理回答の大半が、答えの出た話への重ね投稿・「見当たらない」と伝えるだけの投稿・
URLの ? を質問と数えた誤発動だった）。

  1) 決定論の候補抽出: 疑問符つき（URLの中は数えない）・24〜72時間経過・
     リプライゼロ・未判定（db.unanswered_question_candidates）
  2) LLM判定（安いモデル）: 質問後のチャンネルの流れと質問へのリアクションの
     **中身**を読み、「実質的な質問か」「返事が出ているか（一部でも）」を別々に答えさせる。
     発言があったかどうかでは決めない（無関係な雑談は返事に数えない）。
     拾うのは「実質的な質問」かつ「返事が全く出ていない」ときだけ
  3) 回答役: 流れの中で解決していれば [RESOLVED]、答えの根拠になる記録が
     無ければ [NO_MATERIAL] を出させ、その場合は投稿しない（hold_reason）
  4) シャドー既定: 実投稿せず proactive_log に記録だけ（rescue.shadow=false で
     本投稿を解禁）。本投稿は既存の回答パイプラインで生成し、日次枠を消費する

単体テスト: ./venv/bin/python -m unittest core.test_rescue -v
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
# SQLから多めに取ってから絞る。弾いた候補は台帳に残らないので、取得件数＝返す件数だと
# URLだけの投稿が古い順の枠を72時間ふさぎ続ける
FETCH_LIMIT = 30
# 判定は同じ入力でも揺れるので、「拾う」は2回続けて出たときだけ採る
# （1回目が「拾わない」ならそこで終わるので、追加の呼び出しは拾う寸前の候補だけ）
JUDGE_VOTES = 2
_JSON_RE = re.compile(r"\{.*\}", re.S)

# 修辞疑問・雑談の粗除去（LLM判定前の追加フィルタ。保守的に短文の相槌だけ弾く）
_NOISE_RE = re.compile(r"^(?:え|ほんと|まじ|マジ|そうなの|なるほど)[？?！!〜ー]*$")


_URL_RE = re.compile(r"https?://\S+")
_QUESTION_MARK_RE = re.compile(r"[？?]")


def looks_like_question(content):
    """URLを除いた本文に疑問符があるか（純粋関数）。
    クエリ文字列の ? を質問と数えると、URLの貼り付けに代理回答してしまう。"""
    text = _URL_RE.sub("", content or "").strip()
    return bool(_QUESTION_MARK_RE.search(text)) and not _NOISE_RE.match(text)


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
            exclude_channel_ids=exclude_channel_ids,
            limit=max(limit, FETCH_LIMIT))
    return [r for r in rows if looks_like_question(r["content"])][:limit]


def question_reactions(db_path, candidate):
    """質問に人が付けたリアクション [(絵文字, 表示名)]（判定材料）。
    ✅ や 👀 だけで返事を済ませた質問に、重ねて投稿しないため。"""
    with db.connect(db_path) as conn:
        return db.reactions_for_messages(
            conn, [candidate["id"]]).get(candidate["id"], [])


def followups(db_path, candidate):
    """質問の後に同じchで交わされた発言（判定材料）。"""
    with db.connect(db_path) as conn:
        return db.messages_after(conn, candidate["channel_id"],
                                 after_id=candidate["id"],
                                 limit=FOLLOWUP_MESSAGES)


RESOLVED_MARK = "[RESOLVED]"
NO_MATERIAL_MARK = "[NO_MATERIAL]"
# 回答役への注記。その後の発言が無いときも必ず渡す（無いと材料なしでも投稿する）
ANSWER_NOTE = (
    "これは誰にも頼まれていない代理回答。このチャットの中の事柄についての質問で、答えの根拠に"
    "なる記録（投稿・数字・リンク）が見つからないときは、本文を書かず "
    f"{NO_MATERIAL_MARK} とだけ出力する（「見当たらない」「分からない」と伝える"
    "だけの投稿や、推測での回答はしない）")
FOLLOW_NOTE = (
    ANSWER_NOTE + "。以下は質問の後に同じチャンネルで交わされた発言。すでに済んだこと・"
    "解決したことを未着手のように案内せず、その後の状況を踏まえて答える。"
    "この流れの中で質問への答えが一部でも出ている・解決している場合は、本文を書かず "
    f"{RESOLVED_MARK} とだけ出力する（答えの出た話に重ねて投稿しない）")


def is_resolved_reply(answer):
    """回答役が「その後の流れで解決済み」と判断したか（純粋関数）。
    判定役（軽いモデル）が見落とした解決済みの質問でも、流れを読んだ回答役は
    気づける。その判断で投稿を止める。"""
    return RESOLVED_MARK in (answer or "")


# 投稿しなかった理由 → (rescues.status, proactive_log.action, 記録に残す一言)
HOLD_RECORD = {
    "resolved": ("skipped_answered", "rescue_skip_resolved",
                 "回答役が解決済みと判断"),
    "no_material": ("skipped_no_material", "rescue_skip_no_material",
                    "答える根拠が見つからず見送り"),
}


def hold_reason(answer):
    """回答を投稿しない理由（純粋関数）。投稿してよければ None。
    "resolved"＝その後の流れで解決済み／"no_material"＝答える根拠が無い。
    頼まれた質問なら「分からない」と返す価値があるが、頼まれていない投稿で
    それを言っても通知が増えるだけなので黙る。
    「自信度低め」の脚注の有無では決めない：根拠つきの回答でも細部の1点に付くので、
    脚注で止めると出すべき回答まで止まる。材料の有無は回答役が中身で判断する。"""
    text = (answer or "").strip()
    if is_resolved_reply(text):
        return "resolved"
    if not text or NO_MATERIAL_MARK in text:
        return "no_material"
    return None


def answer_history(follow_rows):
    """回答の生成にも、判定と同じ「その後の流れ」を渡す（純粋関数）。
    質問文だけで答えると、済んだ直後に「今日が締め切りです」のように
    終わった話を未着手のように案内してしまう。"""
    if not follow_rows:
        return [{"author": "（注）", "content": ANSWER_NOTE, "is_bot": False}]
    return [{"author": "（注）", "content": FOLLOW_NOTE, "is_bot": False}] + [
        {"author": r.get("author") or "?", "content": r.get("content") or "",
         "is_bot": bool(r.get("is_bot"))} for r in follow_rows]


ANSWERED_VALUES = ("yes", "partly", "no")


def elapsed_label(question_at, message_at):
    """質問からどれだけ後の発言か（純粋関数）。読めなければ空文字。
    直後の短い一言（「追加します」等）は質問への返事である見込みが高いが、
    時刻が無いと判定役が無関係な発言として読み飛ばす。"""
    try:
        sec = (datetime.fromisoformat(message_at)
               - datetime.fromisoformat(question_at)).total_seconds()
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    if sec < 60:
        return "直後"
    if sec < 3600:
        return f"{int(sec // 60)}分後"
    if sec < 86400:
        return f"{int(sec // 3600)}時間後"
    return f"{int(sec // 86400)}日後"


def build_judge_prompt(candidate, follow_rows, reactions=()):
    """救済判定プロンプト（純粋関数・テスト対象）。
    「質問か」と「返事が出ているか」を別々に答えさせる。1つの true/false に
    まとめさせると、一部だけ返事の出た質問を「未回答」に倒していた。"""
    lines = []
    for r in follow_rows:
        text = (r.get("content") or "").strip().replace("\n", " ")[:200]
        if text:
            when = elapsed_label(candidate.get("created_at"), r.get("created_at"))
            lines.append(f"- {f'（{when}）' if when else ''}"
                         f"{r.get('author') or '?'}: {text}")
    flow = "\n".join(lines) or "（その後の発言なし）"
    reacts = "、".join(f"{e} {n}" for e, n in reactions) or "（なし）"
    return (
        "チャットで24時間以上前に投稿された次の発言について、2点を判定して。\n\n"
        f"【発言】#{candidate['channel']} {candidate['author']}:\n"
        f"「{(candidate['content'] or '').strip()[:400]}」\n\n"
        f"【この発言に付いたリアクション】{reacts}\n\n"
        f"【その後の同チャンネルの流れ】\n{flow}\n\n"
        "1. is_question: 記録を調べれば答えられる、情報を求める実質的な質問か。\n"
        "   次のものは false: 修辞疑問・独り言・つぶやき・雑談・呼びかけ・"
        "予定や作業のメモ・報告・作業の依頼／出欠や参加者の募集、意見・都合・"
        "好みを尋ねる問いかけ（本人たちにしか答えられない）／"
        "特定の個人への私的な問いかけ。\n"
        "2. answered: その後の流れとリアクションの**中身**を読んで決める。\n"
        "   - yes: 質問への返事が出ている、または話が片付いている\n"
        "   - partly: 返事が一部でも出ている／誰かが対応を引き受けた"
        "（「確認します」「追加します」等）／了承・確認中のリアクション"
        "（✅👍👀🙆🫡 等）が付いている／質問者自身が後から答えを書いた\n"
        "   - no: 質問に触れた発言もリアクションも無い\n"
        "   質問の直後に別の人が返した短い一言（「はい」「追加します」「了解です」等）は、"
        "まずこの質問への返事として読む。\n"
        "   発言があるだけでは返事に数えない。質問と関係のない話題は無視する。\n"
        "先に、その後の流れから質問に関係する発言を related に抜き出してから判定する"
        "（最大3件・無ければ空）。\n"
        "迷ったら is_question は false、answered は partly にする"
        "（迷ったら救済しない。誤った拾い上げは信頼を失う）。\n"
        "出力はJSONのみ: {\"related\": [\"発言の抜き出し\"], "
        "\"is_question\": true|false, "
        "\"answered\": \"yes\"|\"partly\"|\"no\", \"reason\": \"一言\"}"
    )


def _no_rescue(reason):
    return {"rescue": False, "is_question": False, "answered": "",
            "reason": reason}


def parse_judge(raw):
    """判定JSONの解釈（純粋関数）。拾うのは「実質的な質問」かつ「返事が全く
    出ていない」ときだけ。壊れた出力・知らない値は救済しない＝安全側。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return _no_rescue("判定不能")
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return _no_rescue("判定不能")
    if not isinstance(data, dict):
        return _no_rescue("判定不能")
    is_question = data.get("is_question") is True
    answered = data.get("answered")
    if answered not in ANSWERED_VALUES:
        return _no_rescue("判定不能")
    return {"rescue": is_question and answered == "no",
            "is_question": is_question, "answered": answered,
            "reason": str(data.get("reason") or "")[:200]}


def judge(candidate, follow_rows, *, model, reactions=(), invoke_fn=None,
          votes=JUDGE_VOTES):
    """LLM判定: 救済すべきか。votes 回すべてが「拾う」のときだけ拾う。
    invoke_fnはテスト差し替え口。"""
    prompt = build_judge_prompt(candidate, follow_rows, reactions)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=JUDGE_TIMEOUT_SEC,
        purpose="rescue").text)
    verdict = _no_rescue("判定不能")
    for _ in range(max(1, votes)):
        verdict = parse_judge(fn(prompt))
        if not verdict["rescue"]:
            break
    return verdict


RESCUE_PREFIX = ("24時間くらい誰も答えてないみたいなので、"
                 "こちらで調べてみました🙋\n")


def record(db_path, message_id, agent_id, status, posted_message_id=None):
    with db.connect(db_path) as conn:
        db.add_rescue(conn, message_id=message_id, agent_id=agent_id,
                      status=status, posted_message_id=posted_message_id,
                      created_at=reminders.fmt(reminders.now_jst()))
