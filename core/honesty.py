#!/usr/bin/env python3
"""「できたフリ」検出器（進化ロードマップ#20）。誠実な失敗の原則をコードで補強する。

2つの検出を行う（発動は marker_actions._apply_honesty_check）:
  1) マーカー不発検出（確定的・本命）: 本文が「登録しました」等の完了を主張して
     いるのに、対応するマーカー実行の -# 行（成功も失敗も）が無い場合、
     「実際には実行されていない」旨の -# 警告を自動付記する。
     嘘が嘘のまま流れることを構造的に防ぐ（過去実例: YouTube要約の本文欠落）。
  2) 根拠なし断定のシャドー計測: 検索ヒット0かつ出典リンク無しの応答に
     断定表現が含まれる場合、投稿には触れず proactive_log に記録だけする
     （実データを見てから注記強化を判断する＝シャドーモード規約）。

検出はすべて純粋関数（テスト対象）。ログ書き込み・本文加工は呼び出し側が行う。
単体テスト: ./venv/bin/python -m unittest test_honesty -v
"""

import re

# 完了主張のパターン（kind -> 主張regex）。マーカー実行の -# 行と突き合わせる
_DONE = r"(?:しました|したっス|しときました|しておきました|完了|済み)"
CLAIMS = {
    "remind": re.compile(r"リマイン[ドダ][^\n]{0,20}?(?:登録|セット|設定)" + _DONE),
    "rule": re.compile(r"ルール[^\n]{0,20}?(?:登録|保存|設定|追加)" + _DONE),
    "capability": re.compile(r"起票" + _DONE),
    # 納期追跡の会話操作。実例2026-08-07「今日の追跡タスクはキャンセルするっス」
    # ＝実行手段が無いまま引き受けた口約束なので、完了形だけでなく約束形も
    # 検出する（マーカーが実行されていれば -# 行＝証拠が必ず付く）。
    # 「納期/追跡」を必須にして、リマインダー等の別機能の取消と混同しない。
    # 疑問形（〜するっスか？）は主張ではないので除外
    "action": re.compile(r"(?:納期|追跡|期日)[^\n]{0,15}?"
                         r"(?:キャンセル|取り消し|取消|削除|完了(?:扱い)?に|"
                         r"変更|延期|リスケ)"
                         r"[^\n]{0,3}?"
                         r"(?:するっス(?!か)|します(?!か)|しとく|しておく|"
                         + _DONE + ")"),
    # 記憶・認識の更新（2026-08-18の実事故: グッズ納期の訂正に「認識更新
    # するっス」と答えて何も保存されなかった）。受け皿（事実台帳）ができた
    # ので、約束形も検出して口約束を封じる。疑問形は主張ではないので除外
    "memory": re.compile(r"(?:認識[^\n]{0,6}?(?:更新|改め|直し|変え)|"
                         r"(?:覚えて|記録して|メモして|反映して)"
                         r"(?:おく|おき)?|"
                         r"以後[^\n]{0,10}?(?:反映|注意|気をつけ))"
                         r"[^\n]{0,3}?"
                         r"(?:するっス(?!か)|します(?!か)|しとく|しておく|"
                         r"おくっス(?!か)|おきます|"
                         + _DONE + ")"),
}
# 実行の証拠は成功／失敗に分けて持つ（kind -> -#行のregex）。
# 「⚠️行が出てるから嘘ではない」だけでは足りない: 失敗しか無いのに本文が
# 完了を主張していると、小さい -# 行を読み飛ばした人間が成功と誤解する
# （実例2026-08-12: APIキーのdeleteスコープ不足で403）。両者を分けて
# 「実行されていない」と「実行したが失敗」を別々に訂正できるようにする。
SUCCESS_DEEDS = {
    "remind": re.compile(r"-# 登録: "),
    "rule": re.compile(r"-# (?:📌 ルール登録|🗑 ルール削除)"),
    "capability": re.compile(r"-# 🧩 能力追加を起票"),
    "action": re.compile(r"-# (?:🗑|📗|📅) 納期追跡"),
    "memory": re.compile(r"-# (?:🧠 事実を記録|🗑 事実を取り消し|"
                         r"📌 ルール登録|🗑 ルール削除|📛 固有名詞を登録|"
                         r"📖 単語帳)"),
}
FAIL_DEEDS = {
    "remind": re.compile(r"-# ⚠️ (?:登録できなかった|宛先)"),
    "rule": re.compile(r"-# ⚠️ (?:ルール登録に失敗|全体共通ルール|id=)"),
    "action": re.compile(r"-# ⚠️ 納期追跡"),
    "memory": re.compile(r"-# ⚠️ (?:事実を記録できなかった|ルール登録に失敗)"),
}
LABELS = {"remind": "リマインダー", "rule": "ルール", "capability": "起票",
          "action": "納期追跡の操作",
          "memory": "認識の更新（事実・ルール・用語の保存）"}
#: 組み込みの検出種別。これ以外の kind は register() で足された外部連携のもの
BUILTIN_KINDS = frozenset(CLAIMS)

# 実挙動が可視かどうか（成功でも失敗でも良い）。成功/失敗の定義から導出して
# 三者がズレないようにする（片方だけ足して検出漏れ、を構造的に防ぐ）。
# register() で書き換わるのでモジュール変数ではなく _rebuild() で作り直す。
DEEDS = {}


def _rebuild():
    DEEDS.clear()
    for kind in CLAIMS:
        patterns = [rx.pattern for rx in (SUCCESS_DEEDS.get(kind),
                                          FAIL_DEEDS.get(kind))
                    if rx is not None]
        # 証拠パターンが1つも無い kind は「常に証拠なし」＝毎回警告になってしまう。
        # 決して一致しない正規表現ではなく、必ず一致する空パターンに倒して
        # 「検出しない」側に寄せる（誤検出より見逃しの方が害が小さい）
        DEEDS[kind] = re.compile("|".join(patterns) if patterns else "")


_rebuild()


def register(kind, claim, success=None, fail=None, label=None):
    """外部連携が自分のマーカーを「できたフリ」検出の対象に加える。

    組み込み以外のマーカー（例: スプレッドシート書き込み）を持つ連携は、
    読み込み時にこれを呼ぶと、本文が完了を主張したのにマーカーの -# 行が
    出ていない場合の自動訂正が効くようになる。

    kind:    一意な識別子（"sheet" 等）
    claim:   完了を主張する本文のパターン（str または compiled regex）
    success: 実行成功時に出る -# 行のパターン（任意）
    fail:    実行失敗時に出る -# 行のパターン（任意）
    label:   人間に見せる名前（未指定なら kind）
    """
    def _rx(value):
        return value if hasattr(value, "search") else re.compile(value)

    CLAIMS[kind] = _rx(claim)
    if success is not None:
        SUCCESS_DEEDS[kind] = _rx(success)
    if fail is not None:
        FAIL_DEEDS[kind] = _rx(fail)
    LABELS[kind] = label or kind
    _rebuild()

# 根拠なし断定（シャドー計測用）。社内事実の言い切りに絞った保守的パターン
ASSERTION_RE = re.compile(
    r"で確定|に決定|と決まって|に決まって|で決まり|が正式に決ま")
LINK_RE = re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/")


def detect_fake_done(answer, skip=()):
    """完了主張があるのに実行の証拠（-#行）が無い kind のリストを返す（純粋関数）。
    skip: 検査しない kind（そのスキルを持たないエージェントの誤検出防止）。"""
    text = answer or ""
    missing = []
    for kind, claim_re in CLAIMS.items():
        if kind in skip:
            continue
        if claim_re.search(text) and not DEEDS[kind].search(text):
            missing.append(kind)
    return missing


# 文の区切り（日本語の終止記号の直後で切る。記号は前の文に残す）
_SENT_SPLIT_RE = re.compile(r"(?<=[。！!？?])")


def strip_claims(answer, kinds):
    """完了を主張している文だけを本文から取り除く（純粋関数）。

    失敗したのに「できました」と書いた文を残したまま注記で否定するのは、
    読み手に矛盾の解決を押し付けている。失敗したなら「できた」と言わない
    のが正しいので、嘘の文自体を消す。-# 行（実行結果の証拠）と、主張を
    含まない他の文はそのまま残す。"""
    claims = [CLAIMS[k] for k in kinds if k in CLAIMS]
    if not claims:
        return answer or ""
    out = []
    for line in (answer or "").split("\n"):
        if line.startswith("-#"):
            out.append(line)          # 実行結果の -# 行は触らない
            continue
        kept = [s for s in _SENT_SPLIT_RE.split(line)
                if not any(rx.search(s) for rx in claims)]
        rebuilt = "".join(kept).strip()
        if rebuilt:
            out.append(rebuilt)
    return "\n".join(out).strip()


def detect_failed_claim(answer, skip=()):
    """完了を主張しているのに、実行結果が失敗しか無い kind を返す（純粋関数）。

    detect_fake_done は「実挙動が可視ならOK」なので、⚠️行が出ていれば通す。
    だが本文が「消しときました！」のままだと、小さい -# 行を読み飛ばした人間は
    成功したと誤解する（実例: APIキーにdeleteスコープが無く403で失敗）。
    失敗の証拠だけがある＝何も反映されていないと確実に言えるので訂正を付ける。"""
    text = answer or ""
    out = []
    for kind, claim_re in CLAIMS.items():
        if kind in skip or kind not in FAIL_DEEDS:
            continue
        if not claim_re.search(text):
            continue
        if FAIL_DEEDS[kind].search(text) and not SUCCESS_DEEDS[kind].search(text):
            out.append(kind)
    return out


def build_failed_claim_note(kinds):
    """本文の【先頭】へ置く失敗の告知（実行したが失敗した場合）。

    人間は1行目を読んで判断するので、最終結果が失敗なら1行目で失敗を伝える
    （通常サイズ＝-#の小さいグレー文字にしない）。嘘の完了主張は
    strip_claims で本文から消してあるので、ここで否定する必要はない。"""
    labels = "・".join(LABELS.get(k) or k for k in kinds)
    return (f"⚠️ 失敗したっス: {labels}はできてないっス"
            "（理由は下の ⚠️ 行っス）。直してからもう一度依頼して"
            "ほしいっス🙇")


def build_fake_done_note(missing):
    """本文の【先頭】へ置く失敗の告知（そもそも実行されていない場合）。
    build_failed_claim_note と同じ理由で、-# ではなく通常サイズの1行目にする。"""
    labels = "・".join(LABELS.get(k) or k for k in missing)
    return (f"⚠️ 失敗したっス: {labels}は実行できてないっス。"
            "お手数ですがもう一度依頼してほしいっス🙇")


def unsourced_assertion(answer, hits):
    """根拠なし断定か（純粋関数・シャドー計測用）。
    検索ヒット0・出典リンク無し・断定表現あり、が揃ったときだけ True。"""
    text = answer or ""
    if hits and hits > 0:
        return False
    if LINK_RE.search(text):
        return False
    return bool(ASSERTION_RE.search(text))


# ひらがなは助詞で全部つながるため除外（カタカナ・漢字・英数の連なりが実質的な名詞）
_TERM_RE = re.compile(r"[ァ-ヶー一-龠a-zA-Z0-9]{2,}")


def extract_assert_terms(excerpt, limit=4):
    """断定箇所から裏取り検索用の語を取り出す（純粋関数）。"""
    seen, out = set(), []
    for w in _TERM_RE.findall(excerpt or ""):
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return out


def verify_assertion(db_path, answer):
    """断定の自動裏取り（RM#85）。社内ログをFTSで検索して裏付けの有無を返す。
    Returns: (verified: bool, excerpt)。claude不使用＝決定論の追加チェック。"""
    from core import search as _search
    ex = assertion_excerpt(answer)
    if not ex:
        return True, ""
    terms = extract_assert_terms(ex)
    if not terms:
        return True, ex
    rows = _search.search_messages(db_path, terms, limit=3)
    return bool(rows), ex


def assertion_excerpt(answer, width=100):
    """シャドー記録用に断定箇所の周辺を切り出す。"""
    text = answer or ""
    m = ASSERTION_RE.search(text)
    if not m:
        return ""
    start = max(0, m.start() - width // 2)
    return text[start:start + width]
