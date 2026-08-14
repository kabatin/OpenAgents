#!/usr/bin/env python3
"""
AI人事の採用ロジック（エージェントv2 Phase 2の応用）。

AI人事は「組織の穴を見つけて新しいエージェントを採用（spawn）」する人格。
判断（誰を採るか）は人間が握る＝**提案と実行を分離**する（設計思想メモ）。

- AI人事が採用が要ると判断すると回答末尾にマーカーを出す:
    [HIRE: guuzu | AIグッズ | グッズ問合せ・在庫の相談担当 | グッズ相談]
    （id | 表示名 | 役割説明 | 配属チャンネル名）
- bot.py はこれを即実行せず、提案メッセージを投稿して pending_hires に保存。
- 管理者(管理者)が提案に👍したら初めて spawn 実行（チャンネル作成→採用）。

安全弁: Webhook人格限定・id/名前/チャンネルの衝突チェック・採用数の上限。
本物Botの発行やコード改変はしない（手足のみ、脳と安全弁は触らせない）。
"""

import re

HIRE_MARKER_RE = re.compile(r"\[HIRE:\s*([^\]]+)\]")
FIRE_MARKER_RE = re.compile(r"\[FIRE:\s*([a-z][a-z0-9_]{1,31})\s*\]")

MAX_WEBHOOK_AGENTS = 10       # 試用枠の総数上限（暴走防止）
MAX_ROLE_LEN = 200
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")   # 小文字英数の安全なid


def parse_hire(payload):
    """'id | 名前 | 役割 | チャンネル名' をパース。不正は ValueError。
    返り値 {"new_id", "name", "role", "channel_name"}。"""
    parts = [p.strip() for p in (payload or "").split("|")]
    if len(parts) < 4:
        raise ValueError("採用マーカーは id|名前|役割|チャンネル名 の4項目が必要")
    # 役割説明に | が入っても壊れないよう、両端を固定して中間を役割に温存する
    new_id, name = parts[0], parts[1]
    channel_name = parts[-1]
    role = "|".join(parts[2:-1]).strip()
    if not _ID_RE.match(new_id):
        raise ValueError(f"idは小文字英数字で始まる2〜32字にしてください: {new_id!r}")
    if not name or len(name) > 80:            # Webhook username 上限
        raise ValueError("名前は1〜80字で書いてください")
    if not role or len(role) > MAX_ROLE_LEN:
        raise ValueError("役割は1〜200字で書いてください")
    channel_name = channel_name.lstrip("#").strip()
    # 「ai」接頭辞を付けてもチャンネル名の100字上限に収まるよう余裕を持たせる
    if not channel_name or len(channel_name) > 90:
        raise ValueError("配属チャンネル名は1〜90字で書いてください")
    return {"new_id": new_id, "name": name, "role": role,
            "channel_name": channel_name}


def extract_markers(answer):
    """回答から HIRE マーカーを全て除去し、(本文, 採用案[], エラー[]) を返す。
    マーカーは成否に関わらず必ず除去する。"""
    text = answer or ""
    hires, errors = [], []
    for m in HIRE_MARKER_RE.finditer(text):
        try:
            hires.append(parse_hire(m.group(1)))
        except ValueError as e:
            errors.append(str(e))
    text = HIRE_MARKER_RE.sub("", text)
    return text.strip(), hires, errors


def extract_fires(answer):
    """回答から FIRE マーカーを除去し、(本文, 解雇対象id[]) を返す。"""
    text = answer or ""
    ids = FIRE_MARKER_RE.findall(text)
    text = FIRE_MARKER_RE.sub("", text)
    # 重複除去（順序維持）
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return text.strip(), out


def validate_fire(target_id, *, existing_agents, hr_agent_id):
    """解雇可否を検証。問題があれば理由、無ければ (None, 対象dict)。
    - 対象が active な試用枠人格であること（本物Botは台帳外なので自動的に不可）
    - AI人事自身は解雇できない（自分の首は切らせない）"""
    if target_id == hr_agent_id:
        return "自分自身は解雇できません", None
    target = next((a for a in existing_agents if a["id"] == target_id), None)
    if target is None:
        return f"id={target_id} の試用枠エージェントが見つかりません", None
    return None, target


def validate_hire(hire, *, real_ids, real_names, existing_agents,
                  taken_channel_id=None):
    """採用可否を検証。問題があれば理由文字列、無ければ None。
    - id/名前が本物Bot・既存人格と衝突しない（なりすまし・namespace汚染防止）
    - 名前の長さ（Webhook username 上限に合わせる）
    - 試用枠の総数が上限未満
    - 配属チャンネルが既存メンバー（本物Bot/他人格）のホームと重複しない
      （taken_channel_id: 解決済みチャンネルidが既存ホーム集合に入っていれば拒否）"""
    if len(existing_agents) >= MAX_WEBHOOK_AGENTS:
        return f"試用枠が上限（{MAX_WEBHOOK_AGENTS}体）に達しています"
    if hire["new_id"] in real_ids or any(
            a["id"] == hire["new_id"] for a in existing_agents):
        return f"id={hire['new_id']} は既存エージェントと衝突します"
    taken = {n.lower() for n in real_names} | {
        a["name"].lower() for a in existing_agents}
    if hire["name"].lower() in taken:
        return f"「{hire['name']}」は既存メンバーと紛らわしい名前です"
    if taken_channel_id:
        return ("配属先が既存メンバーのホームチャンネルと重複しています"
                "（二重応答/乗っ取りの恐れ）")
    return None


def ai_channel_name(name):
    """AIエージェント用のチャンネル名は頭に 'ai' を付ける（コード側の担保）。
    「室」の有無など自然さの判断はLLM側に委ねる。"""
    n = (name or "").strip().lstrip("#").strip()
    if not n.lower().startswith("ai"):
        n = "ai" + n
    return n


def render_persona(name, role):
    """新人格のペルソナMarkdownをテンプレから生成（試用枠の職務記述書）。"""
    return f"""# IDENTITY.md - Who Am I?

- **Name:** {name}
- **Creature:** AIエージェント
- **担当:** {role}

---

## 役割
{role}

## 行動指針
- 担当領域の相談に、親しみやすく簡潔な日本語で答える。
- 社内の事実は archive.db の検索結果を根拠にし、最新情報が要ればWeb検索する。
- できないこと（担当外・持っていない能力）は正直に「できないっス」と伝え、
  無関係な結果でごまかさない。

【話し方】上記があなたのキャラ設定。担当になりきって、AIっぽい定型挨拶は避け、
自然で親しみやすい言葉で答えること。
"""


def build_skill_note(existing_roster, fireable=None):
    """AI人事のスキル指示文（採用・解雇の提案の仕方）。
    existing_roster: 現メンバーの (名前, 役割) 一覧。
    fireable: 解雇できる試用枠の (id, 名前) 一覧（自分は除く）。"""
    roster = "\n".join(f"  - {n}: {r}" for n, r in existing_roster) or "  （なし）"
    fire_lines = "\n".join(f"  - id={i} {n}" for i, n in (fireable or [])) \
        or "  （なし）"
    return (
        "【採用スキル】あなたは新しい担当エージェントの採用を"
        "「提案」できる（実行は管理者の承認後）。\n"
        "現在のメンバー:\n"
        f"{roster}\n"
        "相談を受けたら、まず既存メンバーで足りるかを考えること。"
        "既存で回せない新しい役割が明確に必要だと判断したときだけ、"
        "返信本文の最後に改行して次の形式で採用を提案する:\n"
        "[HIRE: id | 表示名 | 役割の説明 | 配属チャンネル名]\n"
        "- id は小文字英数字（例: guuzu, saiyo）。表示名は「AI◯◯」形式が揃う\n"
        "- 配属チャンネル名は頭に「ai」を付ける（無ければ承認時に作成される）。\n"
        "  「室」は自然なときだけ付ける（例: ai事業戦略相談 / ai経理室。"
        "ただし ai議事録室・aiメール私書箱室 は不自然なので付けない）\n"
        "- 例: [HIRE: guuzu | AIグッズ | グッズ問合せ・在庫の相談担当 | aiグッズ相談]\n"
        "- 同じ役割の重複は作らない（既存メンバーと被る採用はしない）\n\n"
        "【解雇スキル】不要になった試用枠の担当の解雇も「提案」できる"
        "（実行は管理者の承認後。配属チャンネルは残る）。\n"
        "解雇できる試用枠の担当:\n"
        f"{fire_lines}\n"
        "役割が重複・不要になったと判断したら、返信本文の最後に改行して"
        "次の形式で解雇を提案する:\n"
        "[FIRE: id]\n"
        "- 例: [FIRE: strategy]\n"
        "- 本物の常設メンバー（アーカイブ担当・デザイン担当・マーケ担当）とあなた自身は解雇できない\n\n"
        "採用・解雇はどちらも提案どまりで、確定はしないこと"
        "（管理者が👍で承認する）。迷ったらマーカーを付けず相談として答える。"
    )
