#!/usr/bin/env python3
"""
PDF自動要約スキル（決定的プリフック方式）。

メンション無しでチャンネルに投稿されたPDF添付を検知し、claude CLI
（Read許可・cwd=一時dir）で読んで口調付き要約を自動投稿する。
youtube_summary と同じく、発動判定はLLMではなくbot.py側で決定的に行う。
分類・保存・注入ガードは既存の添付読解基盤（attachments.py）を再利用する。

通常フロー（メンション/ホームchのテキスト付き投稿）は answer_question が
既にPDFを読めるため、このスキルは「誰も呼ばれていない投稿」専用。
"""

from core import attachments
from core import search

# 自動発動1回で読む上限。メンション経由（MAX_FILES=5）より控えめにして
# 頼まれていない長文投稿になりすぎないようにする
MAX_PDFS = 3
OVERFLOW_NOTE = f"-# 自動要約は一度に{MAX_PDFS}本までじゃけぇ、残りは省略したっス"


def pick_pdfs(atts):
    """添付からPDFだけを [(att, "pdf")] で抽出（attachments.downloadに直結）。
    (picked, overflow) を返す。サイズ超過PDFは対象外（自動発動なので黙って
    スキップ。メンション経由なら既存フローが「読めない」と正直に答える）。"""
    picked, overflow = [], False
    for att in atts or []:
        kind = attachments.classify(att.filename, att.content_type, att.size)
        if kind != "pdf":
            continue
        if len(picked) >= MAX_PDFS:
            overflow = True
            break
        picked.append((att, "pdf"))
    return picked, overflow


def build_prompt(persona, saved, skipped, user_text):
    """要約プロンプトを組み立てる。ファイル一覧とプロンプトインジェクション
    対策の注意書きは attachments.build_block に委譲する。"""
    lines = [
        f"{persona}あなたはPDF資料の要約係。チャンネルにPDFが投稿されたので、"
        "内容をあなたの口調で分かりやすく要約して共有して。",
        "出力形式:",
    ]
    if len(saved) > 1:
        lines.append("- ファイルごとに「📄 **ファイル名**」の見出しを付ける")
    lines += [
        "- 一言まとめ（1行）",
        "- 要点（箇条書き3〜6個。資料の流れがわかる順で）",
        "- 締めの一言（誰向けの資料かを短く）",
        "資料にない事実を創作しない。全体を1500文字以内。",
        "投稿者の一言があれば要約の観点に反映する。",
        "これはバックエンドの自動処理。音声通知・作業報告・Readでの添付読解"
        "以外のツール実行は一切せず、要約テキストだけを出力すること。",
        "",
        f"【投稿者の一言】{user_text or '（PDFのみ投稿）'}",
        "",
        attachments.build_block(saved, skipped),
    ]
    return "\n".join(lines)


def summarize(persona, saved, skipped, tmpdir, user_text=""):
    """PDF読解→claude CLIで口調付き要約（同期。to_thread経由で呼ぶ）。
    戻り値は「📄 ヘッダ + 要約本文」。"""
    prompt = build_prompt(persona, saved, skipped, user_text)
    body = search.run_claude(prompt, allowed_tools=("Read",), cwd=tmpdir,
                             timeout=attachments.TIMEOUT_SEC)
    names = [s["orig"] for s in saved]
    header = (f"📄 **{names[0]}**" if len(names) == 1
              else f"📄 PDF {len(names)}本の要約")
    return f"{header}\n\n{body}"
