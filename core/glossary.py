#!/usr/bin/env python3
"""単語帳（用語の正誤表）— 誤変換・誤記をチーム全体で恒久修正する（RM#5）。

Whisperの音声認識やLLM生成で生まれた誤表記（例: サマーカップ→夏季大会）は
議事録→決定台帳→回答…と伝染する。単語帳に「誤→正」を一度教えれば:
  1) エージェントの回答本文へ決定論の置換で常時適用（bot._respond）
  2) 議事録生成へ正誤表として注入＋生成結果にも置換適用（meeting-bot）
  3) 検索キーワード抽出へ表記ゆれとして注入（旧表記の過去ログも見つかる）
  4) 決定台帳・納期タスクの既存行も登録時に一括修正（伝染済みの分を遡って直す）

登録はマーカー方式:「夏季大会はサマーカップです」のような訂正を受けた
エージェントが [GLOSSARY: 誤 | 正] を出力→コードが保存（誰でも登録可・
削除 [GLOSSARY_CANCEL: 誤] は管理者のみ）。

このモジュールは meeting-bot からも import される（依存は db と標準ライブラリのみ）。
単体テスト: ./venv/bin/python -m unittest test_glossary -v
"""

import datetime
import re

from core import db

GLOSSARY_MARKER_RE = re.compile(r"\[GLOSSARY:\s*([^\]|]+)\|([^\]]+)\]")
GLOSSARY_CANCEL_RE = re.compile(r"\[GLOSSARY_CANCEL:\s*([^\]]+)\]")
# 固有名詞辞書: [TERM: 正式表記 | 説明(任意)]。正誤表と違い「音が近い未知の
# 誤変換」もLLMが正式表記へ寄せられる（登録済みの誤記だけでなく全変種に効く）
TERM_MARKER_RE = re.compile(r"\[TERM:\s*([^\]|]+)(?:\|([^\]]*))?\]")
TERM_CANCEL_RE = re.compile(r"\[TERM_CANCEL:\s*([^\]]+)\]")
MAX_TERM_LEN = 50
MAX_DESC_LEN = 100
MAX_TERMS_IN_NOTE = 30
# 固有名詞辞書の説明欄に書かれた「Discord ID: アカウント名」（起票#5）。
# 人間が手で登録した対応表なので、機械的な推測より優先できる唯一の根拠。
TERM_DISCORD_ID_RE = re.compile(r"Discord\s*ID[:：]\s*([\w.\-]+)")


def extract_markers(answer):
    """回答から GLOSSARY マーカーを除去し、
    (本文, 追加[(誤,正)...], 取消[誤...], エラー[]) を返す（純粋関数）。"""
    text = answer or ""
    adds, errors = [], []
    for m in GLOSSARY_MARKER_RE.finditer(text):
        wrong, correct = m.group(1).strip(), m.group(2).strip()
        if not wrong or not correct or wrong == correct:
            errors.append(f"単語帳の指定が不正っス: {wrong!r}→{correct!r}")
        elif len(wrong) > MAX_TERM_LEN or len(correct) > MAX_TERM_LEN:
            errors.append(f"単語が長すぎるっス（{MAX_TERM_LEN}字以内）")
        else:
            adds.append((wrong, correct))
    cancels = [c.strip() for c in GLOSSARY_CANCEL_RE.findall(text)
               if c.strip()]
    text = GLOSSARY_MARKER_RE.sub("", text)
    text = GLOSSARY_CANCEL_RE.sub("", text)
    return text.strip(), adds, cancels, errors


def apply(text, pairs):
    """誤→正の決定論置換（純粋関数）。長い誤語から先に置換して
    部分一致の置換事故を防ぐ。"""
    out = text or ""
    for wrong, correct in sorted(pairs, key=lambda p: -len(p[0])):
        out = out.replace(wrong, correct)
    return out


def load_pairs(db_path):
    """有効な (誤, 正) ペア一覧（読めなければ空＝機能は静かに眠る）。"""
    try:
        with db.connect(db_path) as conn:
            return db.glossary_pairs(conn)
    except Exception:
        return []


def save(db_path, wrong, correct, created_by):
    """登録＋伝染済みデータ（決定台帳・納期タスク）の遡及修正。
    Returns: 修正した既存行数。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        db.add_glossary_term(conn, wrong=wrong, correct=correct,
                             created_by=created_by, created_at=now)
        fixed = db.glossary_fix_existing(conn, wrong, correct)
    return fixed


def remove(db_path, wrong):
    with db.connect(db_path) as conn:
        return db.remove_glossary_term(conn, wrong)


def extract_term_markers(answer):
    """回答から TERM マーカーを除去し、
    (本文, 追加[{"term","description"}...], 取消[語...], エラー[]) を返す。"""
    text = answer or ""
    adds, errors = [], []
    for m in TERM_MARKER_RE.finditer(text):
        term = m.group(1).strip()
        desc = (m.group(2) or "").strip()
        if not term:
            errors.append("固有名詞が空っス")
        elif len(term) > MAX_TERM_LEN or len(desc) > MAX_DESC_LEN:
            errors.append(f"固有名詞/説明が長すぎるっス"
                          f"（{MAX_TERM_LEN}/{MAX_DESC_LEN}字以内）")
        else:
            adds.append({"term": term, "description": desc})
    cancels = [c.strip() for c in TERM_CANCEL_RE.findall(text) if c.strip()]
    text = TERM_MARKER_RE.sub("", text)
    text = TERM_CANCEL_RE.sub("", text)
    return text.strip(), adds, cancels, errors


def save_term(db_path, term, description, created_by):
    import datetime as _dt
    now = _dt.datetime.now().isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        db.add_term(conn, term=term, description=description,
                    created_by=created_by, created_at=now)


def remove_term(db_path, term):
    with db.connect(db_path) as conn:
        return db.remove_term(conn, term)


def load_terms(db_path):
    """固有名詞辞書の全エントリ（読めなければ空）。"""
    try:
        with db.connect(db_path) as conn:
            return db.terms_all(conn)
    except Exception:
        return []


def speaker_name_map(terms):
    """固有名詞辞書から {Discordアカウント名: 正式表記(名字)} を作る（純粋関数）。
    議事録・文字起こしの話者表示を名字へ統一するための対応表（起票#5）。
    説明欄に Discord ID が無いエントリ（人物以外）は無視する。"""
    mapping = {}
    for t in terms or []:
        m = TERM_DISCORD_ID_RE.search(t.get("description") or "")
        term = (t.get("term") or "").strip()
        if not m or not term:
            continue
        mapping.setdefault(m.group(1), term)  # 重複は先勝ち（非決定にしない）
    return mapping


def resolve_speaker(account, mapping):
    """話者のDiscordアカウント名を正式表記へ。未登録はそのまま返す
    （知らない人の発言を消したり誤った名前を当てたりしない安全側）。"""
    return (mapping or {}).get(account, account)


def resolve_participants(accounts, mapping):
    """参加者一覧を正式表記へ。順序は維持し、同一人物の重複は畳む。"""
    seen, out = set(), []
    for a in accounts or []:
        name = resolve_speaker(a, mapping)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def build_terms_note(terms):
    """議事録生成プロンプト用の固有名詞リスト（純粋関数・無ければ空文字）。
    正誤表と違い、音が近い**未知の**誤変換もこの正式表記へ寄せさせる。"""
    if not terms:
        return ""
    lines = []
    for t in terms[:MAX_TERMS_IN_NOTE]:
        desc = f"（{t['description']}）" if t.get("description") else ""
        lines.append(f"  - {t['term']}{desc}")
    return ("- 社内の固有名詞一覧（正式表記）。音声認識で音が近い別表記に"
            "なっている箇所は、必ずこの正式表記に直して出力する:\n"
            + "\n".join(lines) + "\n")


def build_terms_context(terms):
    """エージェントの回答Context用の固有名詞辞書（無ければ空文字）。"""
    if not terms:
        return ""
    lines = []
    for t in terms[:MAX_TERMS_IN_NOTE]:
        desc = f": {t['description']}" if t.get("description") else ""
        lines.append(f"- {t['term']}{desc}")
    return ("【社内の固有名詞辞書（正式表記。回答ではこの表記を使い、"
            "音が近い誤記を見たらこれのことだと解釈する）】\n"
            + "\n".join(lines))


def build_skill_note():
    """ルール記憶と同型のスキル指示文（人間の発言にのみ注入）。"""
    return (
        "【固有名詞辞書・単語帳スキル】\n"
        "・利用者が固有名詞の登録を頼んだり、新しい社内固有の名前（イベント名・"
        "会社名・製品名・人名など）を教えてくれたら、返信本文の最後に改行して:\n"
        "[TERM: 正式表記 | 短い説明]（説明が無ければ省略可）\n"
        "・誤記・誤変換の訂正（「XはYです」「Xじゃなくて Y」など表記の訂正）は:\n"
        "[GLOSSARY: 誤った表記 | 正しい表記]\n"
        "どちらも以後の議事録・回答・検索に自動反映される"
        "（本文では登録する旨を一言添える）。方針・行動の指示は [RULE:] に任せ、"
        "迷ったら出力しない。"
    )


def build_correction_table(pairs):
    """議事録生成プロンプトへ注入する正誤表（無ければ空文字・純粋関数）。"""
    if not pairs:
        return ""
    lines = [f"  - 「{w}」→「{c}」" for w, c in pairs[:MAX_TERMS_IN_NOTE]]
    return ("- 社内用語の正誤表（音声認識で左の誤表記になりがち。"
            "必ず右の正しい表記で出力する）:\n" + "\n".join(lines) + "\n")


def synonyms_note(pairs):
    """検索キーワード抽出へ注入する表記ゆれ情報（無ければ空文字・純粋関数）。
    過去ログには誤表記のまま残っている発言があるため、両方で検索させる。"""
    if not pairs:
        return ""
    lines = [f"{c}={w}" for w, c in pairs[:MAX_TERMS_IN_NOTE]]
    return ("・既知の表記ゆれ（同義として両方の表記をキーワードに含める）: "
            + " / ".join(lines) + "\n")
