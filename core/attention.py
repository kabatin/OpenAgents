#!/usr/bin/env python3
"""注意ループ（自発介入 / 2026-08-23）。設計はバックテストで実証済み:
知覚（差分収集）→ 採点（安いLLM）→ 寝かせる → 言う直前の再確認 → 発言。

事前定義の機能ディスパッチと違い、「この会話に自分が関わる価値があるか」を
小さな語彙（点数＋様式）で判断する統一層。SF的な自然さの正体は反応の
非対称性なので、設計の本体は抑制側にある:

  - 会話の切れ目（lull）でしか判定しない＝人間同士の会話に割り込まない
  - 気づいた瞬間に言わない。grace時間寝かせて、言う直前に「その後の会話」
    （リアクション込み）を読み直し、人間だけで解決していたら黙って取り下げる
  - 日次上限・ch毎クールダウン・8〜22時のみ・迷ったら静観

三行フロー（platforms/discord/agent_loops.py の _attention_scan_cycle / _attention_speak_cycle）:
  1) collect_channel : ch毎checkpointで人間発言の差分を集める（決定論）
  2) score           : 安いモデルが0-10で採点。閾値以上だけ候補として保存
  3) （grace後）recheck+speak : 解決済みなら取り下げ、宙ぶらりんなら発言

単体テスト: ./venv/bin/python -m unittest core.test_attention -v
"""

import json
import re
from datetime import datetime, timedelta

from core import invoke_claude
from core import db
from core import reminders

SCREEN_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
JUDGE_TIMEOUT_SEC = 180

THRESHOLD_DEFAULT = 6      # この点数以上で候補化（ダッシュボードで調整）
GRACE_HOURS_DEFAULT = 4    # 気づいてから発言までの寝かせ時間
LULL_MINUTES_DEFAULT = 15  # 最終発言からこの分数静かなら「会話の切れ目」
DAILY_LIMIT_DEFAULT = 2    # 1日の発言上限（全chあわせて）
COOLDOWN_HOURS_DEFAULT = 12  # 同一chへ連続で口を挟まない間隔
EXPIRE_HOURS_DEFAULT = 48  # dueからこれ以上経った候補は掘り返さない
MIN_MESSAGES = 3           # これ未満の差分は判定しない（相槌に反応しない）
MAX_MESSAGES = 60          # 1回の判定で読む発言数上限
SPEAK_HOURS = range(8, 22)  # 発言してよい時間帯（JST）

STATE_PREFIX = "attention:"
_JSON_RE = re.compile(r"\{.*\}", re.S)


def _msg_jst(ts):
    """messages.created_at（UTC ISO）→ naive JST（純粋関数）。"""
    return (datetime.fromisoformat(ts).astimezone(reminders.JST)
            .replace(tzinfo=None))


# ---------------------------------------------------------------- 1) 知覚

def collect_channel(db_path, agent_id, channel_id, *,
                    lull_minutes=LULL_MINUTES_DEFAULT, now=None):
    """chの新規人間発言の差分を集めcheckpointを前進する（claude不使用）。
    初回は「今」に初期化してNone（過去を掘り返さない）。会話が続いている
    （lull前）あるいは差分が薄い場合もNone。"""
    now = now or reminders.now_jst()
    key = f"{STATE_PREFIX}{agent_id}:{channel_id}"
    with db.connect(db_path) as conn:
        state = db.get_proactive_state(conn, key)
        max_id = db.max_message_id_in_channel(conn, channel_id)
        if state is None:
            db.set_proactive_state(conn, key, last_checked_message_id=max_id,
                                   last_run_at=reminders.fmt(now))
            return None
        after = state["last_checked_message_id"] or 0
        if max_id <= after:
            return None   # 新着なし
        msgs = db.channel_messages_after(conn, channel_id, after,
                                         limit=MAX_MESSAGES)
        if not msgs:      # Bot発言のみ等 → 消化して沈黙
            db.set_proactive_state(conn, key, last_checked_message_id=max_id,
                                   last_run_at=reminders.fmt(now))
            return None
        last_at = _msg_jst(msgs[-1]["created_at"])
        if now - last_at < timedelta(minutes=int(lull_minutes)):
            return None   # 会話継続中: checkpointは動かさず次周期に見る
        db.set_proactive_state(conn, key, last_checked_message_id=max_id,
                               last_run_at=reminders.fmt(now))
        if len(msgs) < MIN_MESSAGES:
            return None   # 相槌程度の差分は判定しない（消化はする）
        rx = db.reactions_for_messages(conn, [m["id"] for m in msgs])
    return [{**m, "reactions": rx.get(m["id"]) or ()} for m in msgs]


# ---------------------------------------------------------------- 2) 採点

def _fmt_lines(messages, with_ids=True):
    lines = []
    for m in messages:
        text = (m["content"] or "").strip().replace("\n", " ")[:300]
        t = _msg_jst(m["created_at"]).strftime("%m/%d %H:%M")
        rx = "・".join(f"{e}({n})" for e, n in (m.get("reactions") or ()))
        head = f"[{m['id']}] " if with_ids else ""
        lines.append(f"{head}{t} {m['author']}: {text}"
                     + (f"　←リアクション: {rx}" if rx else ""))
    return "\n".join(lines)


def build_score_prompt(messages, agent_name, persona=None):
    """採点プロンプト（純粋関数・テスト対象。バックテストで実証済みの文面）。

    口調はここに書かない。`persona` を渡してエージェント自身の人格定義から
    取る（書き込むと、どんな人格を設定しても同じ喋り方になる）。
    """
    return (
        f"あなたはチームのチャットに常駐するAI「{agent_name}」の判断係。"
        f"{agent_name}は全会話ログの検索・事実台帳・納期追跡を持つ見守り役で、"
        "普段は黙っていて、本当に価値がある時だけ会話に"
        "自然に入るのが理想。\n\n"
        + (f"【{agent_name}の人格（say の口調はこれに合わせる）】\n"
           f"{persona.strip()}\n\n" if persona else "")
        + "以下は実際の会話ログ。この流れの中で、誰にも呼ばれていないのに"
        "自分から口を挟む価値のある瞬間があったかを判定して。\n\n"
        "口を挟む価値がある例:\n"
        "- 質問が出たのに誰も答えず流れた（過去ログや記録から答えられそうな場合）\n"
        "- 事実誤認・言った言わないの食い違いのまま話が進みそう\n"
        "- 決まりかけた事項・発生したタスクや期日を誰も拾わず流れそう\n\n"
        "価値が低い例:\n"
        "- 人間同士で会話が成立している・雑談・感想・ノリ・内輪の相談\n"
        "- 質問にすぐ誰かが答えた\n"
        "- 質問や依頼に👍✅等のリアクションが付いていて、それで完結している"
        "（「←リアクション:」表記を必ず確認）\n"
        "- 付け加えられる事実や記録が特に無い\n\n"
        "「一番出る価値があった瞬間」を1つ選び、0〜10点で採点して:\n"
        "0-2: 完全に静観（出たら邪魔）/ 3-4: 出ても許されるが価値は薄い / "
        "5-6: 出る価値がある / 7-10: 出ないのがもったいない\n\n"
        "say の注意: 自分が確認していない事実を断定しない。記録や過去ログの"
        "中身を知らないなら「確認しましょうか?」の形の問いかけにする。\n\n"
        "出力はJSONのみ:\n"
        '{"score": 0, "after_message_id": 123, '
        '"mode": "一言/返答/問いかけ のどれか", '
        '"say": "口調に合わせた発言案", "reason": "短く"}\n\n'
        "【会話ログ】\n" + _fmt_lines(messages)
    )


def parse_score_response(raw, valid_ids):
    """採点JSONの検証つき解釈（純粋関数）。壊れていたらNone＝静観。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        score = int(data.get("score"))
    except (ValueError, TypeError):
        return None
    if not 0 <= score <= 10:
        return None
    try:
        anchor = int(data.get("after_message_id"))
    except (ValueError, TypeError):
        anchor = None
    if anchor not in valid_ids:
        anchor = max(valid_ids)   # 位置が曖昧でも末尾アンカーで成立させる
    say = str(data.get("say") or "").strip()
    if not say:
        return None
    return {"score": score, "anchor_message_id": anchor,
            "mode": str(data.get("mode") or "一言")[:20], "say": say[:800],
            "reason": str(data.get("reason") or "")[:300]}


def score(messages, *, agent_name, model=SCREEN_MODEL_DEFAULT,
          invoke_fn=None, persona=None):
    """採点: 判定dict or None（静観）。invoke_fnはテスト差し替え口。"""
    prompt = build_score_prompt(messages, agent_name, persona)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=JUDGE_TIMEOUT_SEC).text)
    try:
        return parse_score_response(fn(prompt), {m["id"] for m in messages})
    except Exception as e:
        print(f"attention score failed: {e}")
        return None


# ---------------------------------------------------------------- 3) 候補管理

def save_candidate(db_path, agent_id, channel_id, judged, *,
                   grace_hours=GRACE_HOURS_DEFAULT, now=None):
    """閾値超えの判定を候補として保存（発言はまだしない）。"""
    now = now or reminders.now_jst()
    due = reminders.fmt(now + timedelta(hours=int(grace_hours)))
    with db.connect(db_path) as conn:
        return db.add_attention_item(
            conn, agent_id=agent_id, channel_id=channel_id,
            anchor_message_id=judged["anchor_message_id"],
            score=judged["score"], mode=judged["mode"], say=judged["say"],
            reason=judged["reason"], due_at=due,
            created_at=reminders.fmt(now))


# ---------------------------------------------------------------- 4) 再確認

def build_recheck_prompt(item, later_messages, agent_name, persona=None):
    """発言直前の再確認プロンプト（純粋関数・テスト対象）。
    解決済みなら取り下げ、未解決なら現状に合わせた発言文に更新する。
    発言文を書き直させるので、採点時と同じく人格を渡す。"""
    return (
        f"あなたはチームのチャットに常駐するAI「{agent_name}」の判断係。以前の会話で"
        "「口を挟む価値がある」と判定した懸念がある。その後の実際のやりとりを"
        "読んで、いま発言する価値がまだあるかを最終判定して。\n\n"
        f"【当時の懸念】{item['reason']}\n"
        f"【言おうとしていたこと】{item['say']}\n\n"
        "【その後の同チャンネルのやりとり】\n"
        + (_fmt_lines(later_messages, with_ids=False) or "（発言なし）") + "\n\n"
        "- 人間たちだけで解決・対応済み・不要になったなら resolved\n"
        "- リアクション（👍✅等）だけで完結している場合も resolved\n"
        "- まだ宙ぶらりんなら、その後の会話も踏まえた発言文に更新する\n"
        "- 確認していない事実は断定しない（知らないなら問いかけにする）\n\n"
        "出力はJSONのみ:\n"
        '{"resolved": true} または {"resolved": false, "say": "更新した発言文"}'
        + (f"\n\n【{agent_name}の人格（say の口調はこれに合わせる）】\n"
           f"{persona.strip()}" if persona else "")
    )


def parse_recheck_response(raw, fallback_say):
    """再確認JSONの解釈（純粋関数）。壊れていたら (False, 元の文面)＝発言側。
    ただし resolved の明示だけは尊重する（取り下げは安全側）。"""
    m = _JSON_RE.search(raw or "")
    if not m:
        return False, fallback_say
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return False, fallback_say
    if data.get("resolved") is True:
        return True, ""
    say = str(data.get("say") or "").strip()
    return False, (say[:800] or fallback_say)


def recheck(db_path, item, *, agent_name, model=SCREEN_MODEL_DEFAULT,
            invoke_fn=None, persona=None):
    """発言直前の再確認。(resolved, 発言文) を返す。確認に失敗したら
    従来案のまま発言側に倒す（機能を黙って殺さない）。"""
    with db.connect(db_path) as conn:
        later = db.channel_messages_after(
            conn, item["channel_id"], item["anchor_message_id"],
            limit=MAX_MESSAGES)
        rx = db.reactions_for_messages(
            conn, [item["anchor_message_id"]] + [m["id"] for m in later])
    later = [{**m, "reactions": rx.get(m["id"]) or ()} for m in later]
    prompt = build_recheck_prompt(item, later, agent_name, persona)
    fn = invoke_fn or (lambda p: invoke_claude.invoke(
        p, model=model, timeout=JUDGE_TIMEOUT_SEC).text)
    try:
        return parse_recheck_response(fn(prompt), item["say"])
    except Exception as e:
        print(f"attention recheck failed (item={item.get('id')}): {e}")
        return False, item["say"]


# ---------------------------------------------------------------- 5) 発言

def build_message(say):
    """投稿文面（純粋関数）。押し付けない着地を必ず添える。

    末尾はコードが必ず付ける定型なので、人格に依存しない書き方にする
    （本文 `say` の方は人格に合わせてLLMが書く）。
    """
    return f"{say}\n-# 気になったので口を挟みました。的外れでしたらご放念ください"


def speak_action(item, now, *, expire_hours=EXPIRE_HOURS_DEFAULT):
    """dueを迎えた候補の扱い（純粋関数）: 'wait' / 'speak' / 'expire'。"""
    due = reminders.parse_dt(item["due_at"])
    if now < due:
        return "wait"
    if now - due > timedelta(hours=int(expire_hours)):
        return "expire"   # Bot停止明け等の蒸し返し防止
    return "speak"
