#!/usr/bin/env python3
"""
YouTube要約スキル（決定的プリフック方式）。

メッセージ中のYouTube URLからInnertube API経由で字幕を直接取得し
（動画ダウンロード不要・APIキー不要）、claude CLIでエージェント口調の
要約を生成する。OpenClaw時代の youtube-summarize スキルの移植。
発動判定はLLMではなくbot.py側の正規表現で決定的に行う。

単体テスト（Discord不要・実ネットワークあり）:
    ./venv/bin/python youtube_summary.py <YouTube URL>
"""

import re
import sys

from core import search

# 動画URLのみ検知。裸の11桁IDは通常チャットの誤爆源になるため不採用。
# <URL> 包み（埋め込み抑止）はURL部分がそのままマッチするので対応不要。
YOUTUBE_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^\s<>]*&)?v=|shorts/|live/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})")
URL_RE = re.compile(r"<?https?://[^\s<>]+>?")

# 字幕の上限。超過時は先頭+末尾を残して中略する
# （動画は結論が末尾に来ることが多いため、頭切りだけにしない）
MAX_TRANSCRIPT_CHARS = 16000
TRIM_HEAD_CHARS = 12000
TRIM_TAIL_CHARS = 4000

LANGS = ("ja", "en")
OEMBED_URL = "https://www.youtube.com/oembed"
OEMBED_TIMEOUT_SEC = 10

MULTI_URL_NOTE = "-# 複数URLがあったけぇ最初の1本だけ要約したっス"


class TranscriptError(Exception):
    """ユーザーにそのまま返せる短文エラー（str(e)が返信文）。"""


# ---------------------------------------------------------------- 純粋ロジック

def extract_video_ids(text):
    """テキスト中のYouTube URLから (video_id, 正規化URL) を出現順に抽出。
    同じ動画IDの重複は最初の1つだけ残す。"""
    found = []
    seen = set()
    for m in YOUTUBE_URL_RE.finditer(text or ""):
        vid = m.group(1)
        if vid in seen:
            continue
        seen.add(vid)
        found.append((vid, f"https://www.youtube.com/watch?v={vid}"))
    return found


def strip_urls(text):
    """テキストからURLを除去して依頼者の一言を取り出す。"""
    return URL_RE.sub("", text or "").strip()


def trim_transcript(text):
    """長すぎる字幕を先頭+末尾を残して中略する。"""
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    return (text[:TRIM_HEAD_CHARS] + "\n…（中略）…\n"
            + text[-TRIM_TAIL_CHARS:])


def build_prompt(persona, title, minutes, user_text, transcript):
    """要約プロンプトを組み立てる。"""
    return (
        f"{persona}あなたはYouTube動画の要約係。依頼者に、以下の字幕から"
        "動画の内容をあなたの口調で分かりやすく要約して。\n"
        "出力形式:\n"
        "- 一言まとめ（1行）\n"
        "- 要点（箇条書き3〜6個。話の流れがわかる順で）\n"
        "- 締めの一言（感想や誰向けの動画かを短く）\n"
        "字幕にない事実を創作しない。全体を1200文字以内。\n"
        "依頼者の一言があれば要約の観点に反映する。\n"
        "これはバックエンドの自動処理。音声通知・ツール実行・作業報告は"
        "一切せず、要約テキストだけを出力すること。\n\n"
        f"【動画タイトル】{title or '不明'}（約{minutes}分）\n"
        f"【依頼者の一言】{user_text or '（URLのみ投稿）'}\n"
        f"【字幕】\n{transcript}")


# ------------------------------------------------------------------------ I/O

def fetch_transcript(video_id):
    """字幕テキストと概算分数を取得。失敗は TranscriptError に翻訳する。
    優先言語（ja,en）が無ければ利用可能な字幕にフォールバック。"""
    # lazy import: 依存未導入でもbot本体・他スキルは動く（imagegenと同方式）
    from youtube_transcript_api import (
        CouldNotRetrieveTranscript, NoTranscriptFound, RequestBlocked,
        TranscriptsDisabled, VideoUnavailable, YouTubeTranscriptApi)
    ytt = YouTubeTranscriptApi()
    try:
        try:
            fetched = ytt.fetch(video_id, languages=list(LANGS))
        except NoTranscriptFound:
            fetched = _fetch_any_language(ytt, video_id)
    except TranscriptsDisabled:
        raise TranscriptError(
            "⚠️ この動画、字幕が無効になっとって要約できんかったっス"
            "（配信中の動画はまだ字幕が無いかもっス）")
    except VideoUnavailable:
        raise TranscriptError("⚠️ この動画、非公開か削除されとるみたいっス")
    except RequestBlocked:
        raise TranscriptError(
            "⚠️ YouTube側に弾かれたっス。しばらくしてから"
            "もう一回頼んでほしいっス")
    except CouldNotRetrieveTranscript as e:
        reason = str(e).strip().replace("\n", " ")[:100]
        raise TranscriptError(f"⚠️ 字幕を取得できんかったっス\n-# {reason}")
    snippets = list(fetched)
    if not snippets:
        raise TranscriptError("⚠️ 字幕が空っぽで要約できんかったっス")
    text = "\n".join(s.text for s in snippets)
    last = snippets[-1]
    minutes = max(1, round((last.start + last.duration) / 60))
    return text, minutes


def _fetch_any_language(ytt, video_id):
    """利用可能な字幕（言語不問）のうち最初に取得できたものを返す。"""
    for t in ytt.list(video_id):
        try:
            return t.fetch()
        except Exception:
            continue
    raise TranscriptError("⚠️ この動画の字幕が見つからんかったっス")


def fetch_title(video_id):
    """動画タイトルをoEmbedで取得（best-effort、失敗時None）。"""
    import requests
    try:
        r = requests.get(
            OEMBED_URL,
            params={"url": f"https://www.youtube.com/watch?v={video_id}",
                    "format": "json"},
            timeout=OEMBED_TIMEOUT_SEC)
        if r.ok:
            return (r.json().get("title") or "").strip() or None
    except Exception:
        pass
    return None


def summarize(persona, video_id, user_text=""):
    """字幕取得→claude CLIで口調付き要約（同期。to_thread経由で呼ぶ）。
    戻り値は「🎬 ヘッダ + 要約本文」。字幕系の失敗は TranscriptError。"""
    transcript, minutes = fetch_transcript(video_id)
    title = fetch_title(video_id)
    prompt = build_prompt(persona, title, minutes, user_text,
                          trim_transcript(transcript))
    body = search.run_claude(prompt)
    header = (f"🎬 **{title}**（約{minutes}分）" if title
              else f"🎬 動画の要約（約{minutes}分）")
    return f"{header}\n\n{body}"


# ------------------------------------------------------------------------ CLI

def main():
    if len(sys.argv) != 2:
        print("usage: python youtube_summary.py <YouTube URL>")
        sys.exit(1)
    found = extract_video_ids(sys.argv[1])
    if not found:
        print("Error: YouTube URLからvideo IDを抽出できませんでした",
              file=sys.stderr)
        sys.exit(1)
    video_id = found[0][0]
    persona = search.load_persona(search.DEFAULT_PERSONA_FILES)
    try:
        print(summarize(persona, video_id))
    except TranscriptError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
