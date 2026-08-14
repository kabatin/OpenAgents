#!/usr/bin/env python3
"""自然文メンションから起票を推定してルーティングする純粋ロジック（テスト対象）。

LLMに「起票一覧＋開発状況＋人格」をsystemで渡し、ユーザー発言に対して
{action, req_id, reply} のJSONを返させる。それを parse_route が安全に正規化する:
- start は req_id が実在の open 起票のときだけ許可（未知id/パース失敗は start させない）
- それ以外は ask / chat に落とす（誤って別の起票を実装しないための安全弁）
"""

import json
import re

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_route_system(persona, status_ctx, open_caps):
    """ルーティング用の system プロンプトを組む（純粋関数）。発言は別途 user で渡す。"""
    caplist = "\n".join(f"#{c['id']}: {c['desc']}" for c in open_caps) \
        or "（未対応の起票なし）"
    return (
        persona
        + "\n\n【未対応の起票一覧（実装できるのはこれだけ）】\n" + caplist
        + "\n\n【今の開発状況】\n" + status_ctx
        + "\n\n【あなたの仕事】ユーザーの発言が『どれかの起票の実装/改修の依頼』かを判断し、"
        "次のJSONだけを出力する（前後に文を付けない）:\n"
        '{"action":"start|ask|chat","req_id":番号 or null,"reply":"つむぎの口調の返信文"}\n'
        "判断基準:\n"
        "- 明確に1件の起票に該当する実装依頼 → action=start, req_id=その番号。"
        "reply は『#Nの〜、着手しますね！』のような短い一言。\n"
        "- 実装依頼だが複数該当/どれか曖昧 → action=ask, req_id=null。"
        "reply で候補の番号と内容を挙げ『どれですか？』と尋ねる。\n"
        "- 実装依頼でない、または該当する起票が無い → action=chat, req_id=null。"
        "reply は普通の会話（無い機能は正直に『まだ無いです』等）。\n"
        "reply 以外（コマンド/コード/ツール呼び出し/調査）は絶対に書かない。")


def parse_route(raw, open_ids):
    """LLM出力を安全な決定に正規化（純粋関数）。start は実在idのみ許可。"""
    fallback = {"action": "chat", "req_id": None, "reply": ""}
    m = _JSON_RE.search(raw or "")
    if not m:
        return fallback
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return fallback
    if not isinstance(obj, dict):
        return fallback
    action = obj.get("action")
    reply = obj.get("reply")
    reply = reply.strip() if isinstance(reply, str) else ""
    req_id = obj.get("req_id")
    if action == "start" and isinstance(req_id, int) and req_id in set(open_ids):
        return {"action": "start", "req_id": req_id, "reply": reply}
    if action == "ask":
        return {"action": "ask", "req_id": None, "reply": reply}
    return {"action": "chat", "req_id": None, "reply": reply}
