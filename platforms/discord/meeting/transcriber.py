#!/usr/bin/env python3
"""
faster-whisperを使った音声文字起こし
"""

import asyncio
import json
import os
import sys
import glob

# 話者の名寄せ: 音声ファイル名のアカウント名を、固有名詞辞書に人間が登録した
# 正式表記へ統一する。会話アーカイブは全BOT共通の1本（core/paths.py）
from core import paths

_ARCHIVE_DB = paths.DB_PATH

# グローバルモデル（起動時に一度だけ読み込む）
_whisper_model = None


def load_speaker_names():
    """{アカウント名: 正式表記} を固有名詞辞書から取得（読めなければ空）。
    空dictなら resolve_speaker は素通しなので、辞書が無い環境でも従来動作。"""
    try:
        from core import glossary
        return glossary.speaker_name_map(glossary.load_terms(_ARCHIVE_DB))
    except Exception as e:
        print(f"⚠️ 話者名の対応表を読めませんでした（アカウント名のまま）: {e}")
        return {}


def resolve_speaker(account, mapping):
    """アカウント名→正式表記。未登録はそのまま（glossary未導入環境でも動く）。"""
    return (mapping or {}).get(account, account)

def get_model():
    """Whisperモデルを取得（シングルトン）。

    faster-whisper は重い依存なので、ここまで来て初めて import する。
    こうすると純粋ロジックのテストや、議事録BOTを使わない環境では
    インストール不要で済む。"""
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper が入っていません。議事録BOTを使うには "
                "meetingbot/requirements.txt をインストールしてください"
            ) from e
        print("🤖 faster-whisper モデル (small) を読み込んでいます...")
        _whisper_model = WhisperModel("small", device="cpu",
                                      compute_type="int8")
    return _whisper_model

def _transcribe_file(model, audio_file, speaker):
    """1ファイルの文字起こし（同期・ワーカースレッド用）

    Returns:
        list of (start_time, speaker, text) tuples
    """
    segments, _ = model.transcribe(
        audio_file,
        language="ja",
        beam_size=5,
        vad_filter=True,
    )
    results = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            results.append((segment.start, speaker, text))
    return results


def _load_timestamps(wav_path):
    """WAVファイルに対応するタイムスタンプファイルを読み込む

    Returns:
        list of float (Unix timestamps) or None
    """
    ts_path = wav_path.rsplit('.', 1)[0] + '_timestamps.json'
    if not os.path.exists(ts_path):
        return None
    try:
        with open(ts_path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️ タイムスタンプ読み込みエラー: {e}")
        return None


def _map_to_real_time(file_time, chunk_timestamps):
    """Whisperのファイル内相対時間を実時間(Unix timestamp)に変換

    Args:
        file_time: Whisperが返したファイル内の秒数
        chunk_timestamps: PCMチャンクごとのUnix timestamp配列（20ms/チャンク）

    Returns:
        float: Unix timestamp
    """
    chunk_idx = int(file_time / 0.02)
    chunk_idx = max(0, min(chunk_idx, len(chunk_timestamps) - 1))
    return chunk_timestamps[chunk_idx]


def build_transcript(all_segments, has_real_timestamps):
    """(time, speaker, text) のリストを時系列順の議事録テキストに整形（純粋関数）。
    発話が1つも無ければ空文字を返す（無音・極端に短い録音でのクラッシュを防ぐ）。
    has_real_timestamps=True なら最初の発話を起点とした経過時間、
    False ならファイル内相対時間で [MM:SS] を付ける。"""
    segments = sorted(all_segments, key=lambda s: s[0])
    if not segments:
        return ""
    base = segments[0][0] if has_real_timestamps else 0
    lines = []
    for start_time, speaker, text in segments:
        elapsed = start_time - base
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60
        lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {text}")
    return "\n".join(lines)


def _cleanup_temp_files(audio_files):
    """WAV・タイムスタンプファイルを削除（ディスク容量節約）。"""
    print("🗑️ 一時ファイルを削除中...")
    for audio_file in audio_files:
        for ext_path in [audio_file,
                         audio_file.rsplit('.', 1)[0] + '_timestamps.json']:
            try:
                if os.path.exists(ext_path):
                    os.remove(ext_path)
                    print(f"   削除: {os.path.basename(ext_path)}")
            except Exception as e:
                print(f"   ⚠️ 削除失敗: {os.path.basename(ext_path)}, {e}")


async def transcribe_audio(audio_dir):
    """
    音声ファイルを文字起こし

    Args:
        audio_dir: 音声ファイルが保存されているディレクトリ

    Returns:
        str: 文字起こし結果
    """
    print(f"📂 音声ファイルを読み込んでいます: {audio_dir}")

    # モデル取得（すでに読み込み済み）
    model = get_model()

    # 話者の名寄せ表（起票#5）。ここで一度だけ引き、全ファイルで使い回す
    speaker_names = load_speaker_names()

    # ディレクトリ内の全音声ファイルを取得
    audio_files = glob.glob(f"{audio_dir}/*.wav")

    if not audio_files:
        print("⚠️ 音声ファイルが見つかりませんでした")
        return "（音声データなし）"

    print(f"🎵 {len(audio_files)}個の音声ファイルを処理します")

    # 全ファイルを文字起こし（タイムスタンプ付きセグメントを収集）
    all_segments = []
    has_real_timestamps = False

    for audio_file in sorted(audio_files):
        basename = os.path.basename(audio_file)
        print(f"  処理中: {basename}")

        # ファイル名からスピーカー名を抽出（形式: {username}_{userid}.wav）
        name_without_ext = basename.rsplit('.', 1)[0]
        parts = name_without_ext.rsplit('_', 1)
        account = parts[0] if len(parts) > 1 and parts[1].isdigit() else name_without_ext
        # 正式表記へ名寄せ（起票#5）。以降の文字起こし・議事録は名字だけを見る
        speaker = resolve_speaker(account, speaker_names)

        # 対応するタイムスタンプファイルを読み込む
        chunk_timestamps = _load_timestamps(audio_file)

        try:
            # ワーカースレッドで実行（イベントループをブロックしない）
            segments = await asyncio.to_thread(_transcribe_file, model, audio_file, speaker)

            if chunk_timestamps:
                # 実時間にマッピング
                has_real_timestamps = True
                for file_time, spk, text in segments:
                    real_time = _map_to_real_time(file_time, chunk_timestamps)
                    all_segments.append((real_time, spk, text))
            else:
                all_segments.extend(segments)

            print(f"    ✅ [{speaker}] {len(segments)}セグメント")

        except Exception as e:
            print(f"    ❌ エラー: {e}")
            continue

    # 整形（純粋関数）。有効な発話が1つも無ければ空文字。
    # 以前はここで all_segments[0] を無条件に触りIndexErrorでクラッシュし、
    # 呼び出し側が「文字起こし失敗」として誤通知していた
    transcript = build_transcript(all_segments, has_real_timestamps)
    if not transcript:
        print("⚠️ 有効な発話が検出されませんでした（無音または極端に短い録音）")
        _cleanup_temp_files(audio_files)
        return ""

    # ファイルに保存
    transcript_file = f"{audio_dir}/transcript.txt"
    with open(transcript_file, 'w', encoding='utf-8') as f:
        f.write(transcript)

    print(f"✅ 文字起こし完了: {transcript_file}")

    _cleanup_temp_files(audio_files)
    return transcript
