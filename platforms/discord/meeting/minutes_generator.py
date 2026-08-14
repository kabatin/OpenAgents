#!/usr/bin/env python3
"""
文字起こしテキストから議事録を生成
"""

import json
import os
import sys
import asyncio

# claude 起動は core/invoke_claude.py に一箇所隔離
from core import invoke_claude
from core import paths

# 単語帳（RM#5）: 音声認識の誤変換をチーム共有の正誤表で恒久修正する
_ARCHIVE_DB = paths.DB_PATH


def _load_glossary():
    """単語帳(誤,正)ペアと固有名詞辞書（読めない環境では空＝静かに眠る）。"""
    try:
        from core import glossary
        return (glossary, glossary.load_pairs(_ARCHIVE_DB),
                glossary.load_terms(_ARCHIVE_DB))
    except Exception:
        return None, [], []

# 議事録生成のデフォルトモデル（config.json の minutes_model で上書き可）
DEFAULT_MINUTES_MODEL = "claude-sonnet-5"
# claude CLI 実行のタイムアウト（秒）。長い会議でも余裕を持たせる。
# 2026-07-03: 約3時間・14万字の会議で300秒では不足したため900秒に延長
CLAUDE_TIMEOUT_SEC = 900


def _load_config():
    """config.json を読み込む（失敗時は空dict）"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _load_user_mapping():
    """config.jsonからユーザー名マッピングを読み込む"""
    return _load_config().get('user_mapping', {})


def build_mapping_instruction(user_mapping, speaker_names):
    """TODO担当者のメンション化対応表を組み立てる（純粋関数）。

    左辺は文字起こしに実際に現れる表記に合わせる。話者の名寄せ（起票#5）で
    文字起こしが名字になったため、対応表もアカウント名でなく名字を見出しにする
    （名寄せ前の挙動＝アカウント名のままも speaker_names が空なら維持される）。
    同じ人の重複行は畳む。"""
    if not user_mapping:
        return ""
    lines, seen = [], set()
    for account, mention in user_mapping.items():
        label = (speaker_names or {}).get(account, account)
        if label in seen:
            continue
        seen.add(label)
        lines.append(f"  - {label} → {mention}")
    return (
        "- TODOの担当者は次の対応表で、右側の値（Discordメンション <@…> 形式）を"
        "そのままコピーして使う（参加者一覧や本文中の人名はテキストのままでよい）：\n"
        + "\n".join(lines)
        + "\n  対応表にないユーザーはテキストのまま使用。\n"
    )


def _run_claude(prompt, model):
    """
    claude を runner 経由でヘッドレス実行し、生成結果を返す（同期・ブロッキング）。
    プロンプトはstdin経由・失敗時は例外送出（挙動は従来と同じ）。
    """
    return invoke_claude.invoke(
        prompt,
        model=model,
        timeout=CLAUDE_TIMEOUT_SEC,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    ).text

async def generate_minutes_async(transcript, start_time, end_time, participants):
    """
    文字起こしから議事録を生成（claude CLI をヘッドレス実行）

    Args:
        transcript: 文字起こしテキスト
        start_time: 会議開始時刻
        end_time: 会議終了時刻
        participants: 参加者リスト

    Returns:
        str: 整形された議事録
    """
    print("🤖 Claude で議事録を生成中...")

    # 単語帳（正誤表＋固有名詞辞書）: プロンプトで誘導し、生成後にも決定論で置換
    gmod, gpairs, gterms = _load_glossary()
    glossary_instruction = ""
    speaker_names = {}
    if gmod:
        glossary_instruction = (gmod.build_terms_note(gterms)
                                + gmod.build_correction_table(gpairs))
        speaker_names = gmod.speaker_name_map(gterms)

    # ユーザー名マッピング（TODO担当者を Discord メンション化するための対応表）
    mapping_instruction = build_mapping_instruction(
        _load_user_mapping(), speaker_names)

    # 会議メタ情報
    weekday = ['月', '火', '水', '木', '金', '土', '日'][start_time.weekday()]
    date_str = f"{start_time.strftime('%Y/%m/%d')}（{weekday}）"
    duration_min = max(1, round((end_time - start_time).total_seconds() / 60))
    time_str = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}（約{duration_min}分）"
    # 参加者も名字へ名寄せ（起票#5）。話者ラベルは transcriber 側で変換済みなので、
    # ここを揃えると議事録内の人名表記が全て正式表記に一本化される
    if gmod:
        participants = gmod.resolve_participants(
            participants, gmod.speaker_name_map(gterms))
    participants_str = ', '.join(participants) if participants else '（記録なし）'

    # プロンプト作成（トピック完結型・Slack mrkdwn を直接出力）
    prompt = f"""あなたは優秀な議事録作成アシスタントです。
以下はDiscord定例会議の文字起こし（音声認識のため誤字・聞き間違い・フィラーが大量）。
これを「読みやすいSlack議事録」に変換してください。

【会議メタ情報】
- 日付: {date_str}
- 時間: {time_str}
- 参加者: {participants_str}

【内容ルール（最重要）】
- 音声認識の誤変換を文脈から推測して正す。意味の通らない断片・相槌・雑談・フィラーは捨てる。
- 聞き取れない箇所を無理に含めない。固有名詞など事実が不確かな点は「（要確認）」と明記。
- 話題ごとにまとめ、トピックは3〜7個程度に集約する。

【表記ルール（最重要・厳守）】
- Discordのマークダウン記法で“直接”出力する。太字は **アスタリスク2個** で囲む
  （アスタリスク1個 *…* はDiscordでは斜体になるので太字には使わない）。
- 箇条書きは • 、区切り線は ━━━━━━━━━━━━━━━━━━ 。
- ★横長厳禁★: 各行は全角80文字以内に収める。
  80文字を超えそうな文は「、」または「。」の直後で必ず改行する。短い文はそのまま1行でよい。
  箇条書きが長い場合も読点・句点で改行して複数行に分ける（継続行は • を付けず全角スペース1つで字下げ）。
{glossary_instruction}{mapping_instruction}- 余計な前置き・後書きは出力しない（議事録本文のみ）。

【出力フォーマット = トピック完結型】
冒頭にヘッダーとサマリー、その後トピックごとに「背景→決定→TODO」を完結させる。
以下の構成で出力する（絵文字・トピック数・各行の内容は会議に合わせる）：

📋 **定例会議 {date_str}**
🕐 {time_str} ｜ 参加: {participants_str}

> 会議全体の要約を2〜3行（最重要の決定と急ぎTODOに触れる。各行80文字以内）。

━━━━━━━━━━━━━━━━━━

**1. 🎮 トピック名**
• 背景・論点（1要点ずつ、長ければ句読点で改行）
✅ 決定事項（あれば。なければこの行を省略）
☐ TODO: 内容（担当: <@メンション> ／ 期限: いつ）
🔴 緊急のTODOは行頭を🔴にする

（トピックごとに ━ の区切り線を挟んで繰り返す）

---
【文字起こし（元データ）】
{transcript}
"""

    model = _load_config().get('minutes_model', DEFAULT_MINUTES_MODEL)

    try:
        # claude CLI をヘッドレス実行（ゲートウェイ非依存・手動運用と同じ認証経路）。
        # ブロッキングなsubprocessは別スレッドで実行し、呼び出し側のイベントループを止めない。
        reply = await asyncio.to_thread(_run_claude, prompt, model)
        if gmod and gpairs:
            reply = gmod.apply(reply, gpairs)   # 正誤表の決定論適用（最終保証）
        return reply
    except Exception as e:
        # 生成失敗時は例外を送出する。
        # 黙って文字起こしそのままを返すと、呼び出し側（bot.py）が「成功」と誤認し
        # 低品質な投稿＋pending_minutes.jsonの削除でフォールバック（手動復旧）が機能しなくなるため。
        print(f"❌ 議事録生成エラー: {e}")
        raise

def generate_minutes(transcript, start_time, end_time, participants):
    """
    同期版ラッパー（既存コードとの互換性のため）
    """
    return asyncio.run(generate_minutes_async(transcript, start_time, end_time, participants))
