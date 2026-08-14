#!/usr/bin/env python3
"""
Discord Meeting Bot for エージェント1
「ラウンジ」VCへの入室を検知して自動録音し、文字起こし→議事録生成→
Discord Webhook投稿まで自動で行う。失敗した議事録は pending_minutes.json
としてフラグが残り、Bot自身の定期リトライで再処理される。
"""

import sys
import os
import logging

# 標準出力のバッファリングを無効化
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
os.environ['PYTHONUNBUFFERED'] = '1'

import ssl
import certifi
import discord
from discord.ext import commands, voice_recv
import asyncio
import glob
import json
import os
from datetime import datetime
import wave
import io
import time
import shutil
import threading
from collections import deque

# 起動時にimportして遅延を避ける
from core import config as app_config
from platforms.discord.meeting.transcriber import transcribe_audio
from platforms.discord.meeting.discord_notifier import post_to_discord
from platforms.discord.meeting.minutes_generator import generate_minutes

# Whisperモデルは初回使用時に自動読み込み（起動時には読み込まない）

# SSL証明書設定
ssl_context = ssl.create_default_context(cafile=certifi.where())

# 設定はリポジトリ直下の config.json 1枚（meeting_bot セクション）。
# トークンは環境変数参照（"${VAR}"）でも書ける（core/config.py）。
_app_config = app_config.load()
config = _app_config.get('meeting_bot') or {}
config.setdefault('guild_id', _app_config.get('guild_id'))

DISCORD_TOKEN = (os.environ.get('DISCORD_MEETINGBOT_TOKEN')
                 or config.get('token'))
if not DISCORD_TOKEN:
    raise RuntimeError(
        '議事録BOTのトークンが未設定です。'
        'ダッシュボードの「議事録BOT」から設定してください'
        '（config.json の meeting_bot.token）'
    )

# Bot初期化
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# グローバル変数
is_recording = False
audio_sink = None
meeting_start_time = None
participants = set()
voice_client = None
recording_dir = None

# Opus Decoderの設定（discord.pyの標準）
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16bit
SAMPLE_RATE = 48000

def _buffer_base_name(user, user_id):
    """WAV/中間ファイルの共通ベース名（形式は transcriber の話者抽出に合わせる）。"""
    return f"{user.name}_{user_id}"


def append_pcm_chunk(recording_dir, base_name, pcm_bytes, timestamps):
    """PCMを生バイト(.pcm)へ、対応するタイムスタンプを .jsonl へ追記する。

    チェックポイントごとに『新規ぶんだけ』追記するため O(新規) で済み、
    会議が長引いてもイベントループを止めない（旧 save_audio_files は毎回
    全バッファを結合・再書き出ししていた＝長会議で線形に重くなる原因）。
    """
    if not pcm_bytes:
        return
    with open(f"{recording_dir}/{base_name}.pcm", 'ab') as f:
        f.write(pcm_bytes)
    with open(f"{recording_dir}/{base_name}_timestamps.jsonl", 'a') as f:
        for t in timestamps:
            f.write(json.dumps(t) + '\n')


def finalize_recording_files(recording_dir, base_name):
    """会議終了時に一度だけ .pcm→.wav / .jsonl→_timestamps.json に確定する。

    transcriber は .wav と _timestamps.json（配列）を読むため、増分の中間
    ファイルをここで最終形へ変換し、中間ファイルは掃除する。PCMが一度も
    無いユーザー（無音）は何も作らない。stop時の1回だけなので O(全体) で許容。
    """
    pcm_path = f"{recording_dir}/{base_name}.pcm"
    jsonl_path = f"{recording_dir}/{base_name}_timestamps.jsonl"
    if not os.path.exists(pcm_path):
        return
    with open(pcm_path, 'rb') as f:
        raw = f.read()
    wav_path = f"{recording_dir}/{base_name}.wav"
    with wave.open(wav_path, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    timestamps = []
    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    timestamps.append(json.loads(line))
    with open(f"{recording_dir}/{base_name}_timestamps.json", 'w') as f:
        json.dump(timestamps, f)
    duration = len(raw) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
    print(f"  💾 {wav_path} ({duration:.1f}秒, {len(raw)} bytes)", flush=True)
    for tmp in (pcm_path, jsonl_path):
        try:
            os.remove(tmp)
        except OSError:
            pass


class MeetingSink(voice_recv.AudioSink):
    """会議録音用カスタムSink"""

    def __init__(self, vc=None):
        super().__init__()
        self.vc = vc
        self.audio_buffers = {}  # {user_id: {...}} ※下記 _ensure_user 参照
        self.participants = set()
        self.decoders = {}  # {user_id: OpusDecoder}
        # 受信スレッド(write)とチェックポイントスレッド(flush)が同じ
        # audio_buffers を触るため、辞書の構造変更（新規ユーザー追加）だけを
        # 直列化する。チャンクの append/popleft は deque が両端スレッドセーフ。
        self._buffers_lock = threading.Lock()
        print("📝 MeetingSink初期化完了", flush=True)

    def _ensure_user(self, user):
        """未知ユーザーのバッファを用意する（構造変更なのでロック内で）。
        opus_chunks は廃止し、生データを溜めずカウンタ(chunk_count)だけ持つ
        （旧実装は使い道のない opus 生データを会議中ずっと蓄積していた）。"""
        if user.id in self.audio_buffers:
            return
        with self._buffers_lock:
            if user.id not in self.audio_buffers:
                self.audio_buffers[user.id] = {
                    'user': user,
                    'chunk_count': 0,
                    'pcm_chunks': deque(),
                    'pcm_timestamps': deque(),
                }
                self.participants.add(user.name)
                print(f"👤 新規参加者: {user.name} (ID: {user.id})", flush=True)

    def wants_opus(self):
        """Opusデータを直接受け取る（デコードエラー回避）"""
        return True

    def write(self, user, data):
        """音声データ受信コールバック（Opusデータを受信）"""
        if user and data.opus:
            user_id = user.id
            from discord.opus import Decoder as OpusDecoder

            self._ensure_user(user)
            buf = self.audio_buffers[user_id]

            # デコーダが無ければ生成（cleanup()後の再接続時もこの経路で復旧する）
            if user_id not in self.decoders:
                self.decoders[user_id] = OpusDecoder()

            # DAVE復号: voice_recvが渡すdata.opusはDAVE暗号化されたまま
            try:
                from davey import MediaType
                dave_session = self.vc._connection.dave_session
                opus_data = dave_session.decrypt(user.id, MediaType.audio, data.opus)
            except Exception:
                opus_data = data.opus  # DAVE復号失敗時はそのまま試行

            # デコードしてPCMだけ保持（opus生データは破棄＝メモリ節約）
            now = time.time()
            try:
                pcm = self.decoders[user_id].decode(opus_data)
                buf['pcm_chunks'].append(pcm)
                buf['pcm_timestamps'].append(now)
            except Exception as e:
                # デコードエラーは無視して続行
                print(f"⚠️ デコードエラー（スキップ）: {user.name}, {e}", flush=True)

            # 10チャンクごとにログ（約0.2秒）
            buf['chunk_count'] += 1
            chunk_count = buf['chunk_count']
            if chunk_count % 10 == 0:
                duration = chunk_count * 0.02  # 20ms/chunk
                print(f"🎤 {user.name}: {chunk_count}チャンク ({duration:.1f}秒)", flush=True)

    def cleanup(self):
        """クリーンアップ処理

        注意: decoders はクリアしない。voice_recv は stop_listening / 再 listen
        でこの cleanup を呼び出すため、ここで decoders を破棄すると
        再接続後の write() で KeyError になり PCM が一切取れなくなる。
        decoders は MeetingSink インスタンスのライフタイム終了時に
        自動で GC される。
        """
        print("🧹 MeetingSink クリーンアップ (decoders/buffers は保持)", flush=True)

    def flush_to_disk(self, recording_dir):
        """各ユーザーの未書き込みPCMを中間ファイルへ追記し、メモリを解放する。

        チェックポイント（watchdogが30秒ごと・to_thread経由）から呼ぶ。
        deque の popleft は右端 append と競合しないため受信を止めない。
        書き出したチャンクはメモリから抜けるので会議中のバッファ肥大を防ぐ。
        """
        with self._buffers_lock:
            items = list(self.audio_buffers.items())
        for user_id, data in items:
            pcm_q = data['pcm_chunks']
            ts_q = data['pcm_timestamps']
            # 対応が取れている分だけ（末尾の書きかけを避けるため min を取る）
            n = min(len(pcm_q), len(ts_q))
            if n == 0:
                continue
            pcm_bytes = bytearray()
            timestamps = []
            for _ in range(n):
                pcm_bytes += pcm_q.popleft()
                timestamps.append(ts_q.popleft())
            base_name = _buffer_base_name(data['user'], user_id)
            append_pcm_chunk(recording_dir, base_name, bytes(pcm_bytes), timestamps)

    def finalize(self, recording_dir):
        """残りをflushし、全ユーザーのWAV/タイムスタンプを確定する（stop時1回）。"""
        self.flush_to_disk(recording_dir)
        with self._buffers_lock:
            items = list(self.audio_buffers.items())
        print(f"💾 音声保存開始: {len(items)} ユーザー", flush=True)
        for user_id, data in items:
            base_name = _buffer_base_name(data['user'], user_id)
            finalize_recording_files(recording_dir, base_name)
        print(f"✅ 音声保存完了: {len(items)} ファイル", flush=True)

def should_end_meeting(non_bot_count, empty_polls, threshold=2):
    """接続復帰後、対象VCが連続して無人かを判定する（純粋関数）。

    Returns:
        (new_empty_polls, should_end)
        非botが居れば連続カウントを0に戻す。無人が threshold 回続いたら終了。
        一時的な0人（全員が再接続中など）での誤終了を避けるため連続回数で見る。
    """
    if non_bot_count > 0:
        return 0, False
    new_polls = empty_polls + 1
    return new_polls, new_polls >= threshold


# 録音ディレクトリの自動prune（問題5: recordings 溜まりっぱなし）
RECORDINGS_ROOT = 'recordings'
RECORDING_MAX_AGE_DAYS = 14
# 未処理を示すファイル。これらが残るdirは古くても消さない（手動対応の余地を残す）
PENDING_MARKERS = ('pending_minutes.json', 'pending_minutes.json.failed')


def stale_recording_dirs(entries, now_ts, max_age_days):
    """削除対象の録音ディレクトリを選ぶ（純粋関数）。

    Args:
        entries: [(path, mtime_ts, has_pending)] のリスト
        now_ts: 現在の Unix time
        max_age_days: これを『超えた』ものが対象（ちょうどは保持）
    Returns:
        削除対象 path のリスト（has_pending=True は古くても除外）
    """
    cutoff = max_age_days * 86400
    return [path for path, mtime, has_pending in entries
            if not has_pending and (now_ts - mtime) > cutoff]


def prune_old_recordings():
    """RECORDING_MAX_AGE_DAYS を超えた録音dirを削除する（未処理dirは保護）。"""
    if not os.path.isdir(RECORDINGS_ROOT):
        return
    entries = []
    for name in os.listdir(RECORDINGS_ROOT):
        path = os.path.join(RECORDINGS_ROOT, name)
        if not os.path.isdir(path):
            continue
        has_pending = any(os.path.exists(os.path.join(path, m))
                          for m in PENDING_MARKERS)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        entries.append((path, mtime, has_pending))
    for path in stale_recording_dirs(entries, time.time(), RECORDING_MAX_AGE_DAYS):
        try:
            shutil.rmtree(path)
            print(f"🗑️ 古い録音を削除しました（{RECORDING_MAX_AGE_DAYS}日超）: {path}",
                  flush=True)
        except Exception as e:
            print(f"⚠️ 録音削除に失敗: {path}: {e}", flush=True)


# pending議事録のリトライ（エージェントv2 Phase 0）。
# 旧フォールバック（OpenClaw heartbeat）は基盤廃止で実行主体が消えたため、
# Bot自身が定期的に pending_minutes.json を拾って再処理する。
# 注意: MIN_AGE は minutes_generator.CLAUDE_TIMEOUT_SEC(900) より十分大きく保つ
# （インライン生成中のフラグを拾わないための時間ガード。確実な排他は
#  _inflight_minutes が担う）。
PENDING_RETRY_MIN_AGE_SEC = 1800
PENDING_RETRY_INTERVAL_SEC = 3600
PENDING_RETRY_MAX_FAILURES = 5  # 超えたら .failed へ退避（無限リトライ遮断）

pending_retry_task = None
# インライン生成が進行中の録音dir（リトライループとの二重投稿を排他）
_inflight_minutes = set()
# flagパス -> 連続失敗回数（プロセス内カウント。再起動リセットは許容）
_pending_failures = {}


async def process_pending_minutes():
    """残っている pending_minutes.json を再処理する（1件ずつ・best-effort）。"""
    for flag_file in sorted(glob.glob('recordings/*/pending_minutes.json')):
        rec_dir = os.path.dirname(flag_file)
        if rec_dir in _inflight_minutes:
            continue  # stop_meeting のインライン生成が進行中
        try:
            if time.time() - os.path.getmtime(flag_file) < PENDING_RETRY_MIN_AGE_SEC:
                continue
            transcript_path = os.path.join(rec_dir, 'transcript.txt')
            if not os.path.exists(transcript_path):
                raise RuntimeError("transcript.txt が見つかりません")
            with open(flag_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            with open(transcript_path, 'r', encoding='utf-8') as f:
                transcript = f.read()
            print(f"🔁 pending議事録を再処理します: {rec_dir}", flush=True)
            minutes = await asyncio.to_thread(
                generate_minutes,
                transcript,
                datetime.fromisoformat(meta['start_time']),
                datetime.fromisoformat(meta['end_time']),
                meta.get('participants') or [],
            )
            await post_to_discord(minutes)
            os.remove(flag_file)
            _pending_failures.pop(flag_file, None)
            print(f"✅ pending議事録の再投稿が完了しました: {rec_dir}", flush=True)
        except Exception as e:
            count = _pending_failures.get(flag_file, 0) + 1
            _pending_failures[flag_file] = count
            print(f"⚠️ pending議事録の再処理に失敗 "
                  f"({count}/{PENDING_RETRY_MAX_FAILURES}): {flag_file}: {e}",
                  flush=True)
            if count >= PENDING_RETRY_MAX_FAILURES:
                try:
                    os.rename(flag_file, flag_file + '.failed')
                    print(f"⛔ 連続失敗のため退避しました（手動対応が必要）: "
                          f"{flag_file}.failed", flush=True)
                except Exception:
                    pass


async def _pending_minutes_loop():
    """1時間ごとに pending をチェック。録音中は触らない。"""
    await asyncio.sleep(60)  # 起動直後を避ける
    while True:
        try:
            if not is_recording:
                await process_pending_minutes()
                # 古い録音の掃除（未処理dirは保護）。I/Oは別スレッドで。
                await asyncio.to_thread(prune_old_recordings)
        except Exception as e:
            print(f"⚠️ pendingリトライループで例外: {e}", flush=True)
        await asyncio.sleep(PENDING_RETRY_INTERVAL_SEC)


@bot.event
async def on_ready():
    """Bot起動時。起動時点で対象チャンネルに既に人が居れば即参加する"""
    print(f'✅ {bot.user} としてログインしました')
    print(f'Guild ID: {config["guild_id"]}')

    global pending_retry_task
    if pending_retry_task is None:
        pending_retry_task = asyncio.ensure_future(_pending_minutes_loop())
        print("🔁 pending議事録リトライループを開始しました", flush=True)

    # 起動時に対象ボイスチャンネルの在室状況をチェック
    try:
        if is_recording or (voice_client and voice_client.is_connected()):
            return  # 既に接続中ならスキップ

        guild = bot.get_guild(int(config['guild_id']))
        if not guild:
            return
        voice_channel = discord.utils.get(guild.voice_channels, name=config['voice_channel_name'])
        if not voice_channel:
            return

        humans = [m for m in voice_channel.members if not m.bot]
        if humans:
            print(f"🚀 起動時に {len(humans)}人 が {voice_channel.name} に在室中。自動参加します。", flush=True)
            asyncio.ensure_future(auto_join_meeting())
        else:
            print(f"ℹ️ 起動時 {voice_channel.name} は無人。待機します。", flush=True)
    except Exception as e:
        print(f"⚠️ on_ready 自動参加チェック失敗: {e}", flush=True)
        import traceback
        traceback.print_exc()

@bot.event
async def on_voice_state_update(member, before, after):
    """ボイスチャンネルの入退出を監視（参加検知＆退出検知）"""
    global voice_client, is_recording

    # Botのイベントは無視
    if member.bot:
        return

    # チャンネル移動がない場合（ミュート切替等）は無視
    if before.channel == after.channel:
        return

    target_channel_name = config['voice_channel_name']

    # 誰かがターゲットチャンネルに新しく参加 → Bot未参加なら自動参加
    if (after.channel and after.channel.name == target_channel_name and
        (not before.channel or before.channel.id != after.channel.id)):
        if not is_recording and not (voice_client and voice_client.is_connected()):
            print(f"👀 {member.name} が {target_channel_name} に参加。自動参加します。", flush=True)
            asyncio.ensure_future(auto_join_meeting())
            return

    # 録音中でない場合は退出監視不要（ただし残留接続のクリーンアップは行う）
    if not is_recording or not voice_client:
        # 再接続で残留している場合はクリーンアップ
        if bot.voice_clients:
            print("🧹 録音終了後に残留ボイス接続を検出。クリーンアップします。", flush=True)
            for vc in bot.voice_clients:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        return

    # ターゲットチャンネルから退出した場合（別チャンネルへの移動も含む）
    if before.channel and before.channel.id == voice_client.channel.id:
        if not after.channel or after.channel.id != before.channel.id:
            members = [m for m in voice_client.channel.members if not m.bot]
            print(f"👋 {member.name} がボイスチャンネルから退出しました（残り{len(members)}人）", flush=True)

            if len(members) == 0:
                print("⚠️ 参加者が全員退出しました。会議を終了します。", flush=True)
                await stop_meeting()

async def auto_join_meeting():
    """誰かがVCに参加した時に自動で会議開始"""
    global voice_client, is_recording

    # 既に接続中・録音中なら何もしない（二重参加防止）
    if is_recording or (voice_client and voice_client.is_connected()):
        print("ℹ️ 既に接続中のためスキップ", flush=True)
        return

    guild = bot.get_guild(int(config['guild_id']))
    if not guild:
        print(f"❌ Guild {config['guild_id']} が見つかりません")
        return

    voice_channel = discord.utils.get(guild.voice_channels, name=config['voice_channel_name'])
    if not voice_channel:
        print(f"❌ ボイスチャンネル '{config['voice_channel_name']}' が見つかりません")
        return

    try:
        voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        print(f"✅ {voice_channel.name} に参加しました")

        await start_recording()

    except Exception as e:
        print(f"❌ 接続エラー: {e}")
        import traceback
        traceback.print_exc()

async def start_recording():
    """録音開始"""
    global is_recording, meeting_start_time, participants, recording_dir, audio_sink
    
    if is_recording:
        print("⚠️ すでに録音中です")
        return
    
    is_recording = True
    meeting_start_time = datetime.now()
    participants = set()
    
    # 録音ディレクトリ作成
    timestamp = meeting_start_time.strftime('%Y%m%d_%H%M%S')
    recording_dir = f"recordings/{timestamp}"
    os.makedirs(recording_dir, exist_ok=True)
    
    # ボイスチャンネルの参加者を記録
    if voice_client and voice_client.channel:
        print(f"📡 接続先チャンネル: {voice_client.channel.name}", flush=True)
        all_members = voice_client.channel.members
        print(f"📡 全参加者 ({len(all_members)}人): {[f'{m.name}(bot={m.bot})' for m in all_members]}", flush=True)
        
        for member in all_members:
            if not member.bot:
                participants.add(member.name)
                print(f"👥 初期参加者（人間）: {member.name}", flush=True)
    
    # MeetingSinkを作成して録音開始（DAVE復号のためvcを渡す）
    audio_sink = MeetingSink(vc=voice_client)
    voice_client.listen(audio_sink)

    print(f"🎙️ 録音開始: {meeting_start_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📁 保存先: {recording_dir}", flush=True)
    print(f"🎯 録音中... （話してください）", flush=True)

    # 録音リスナー監視を開始（再接続時にlistenが外れる問題への対策）
    asyncio.ensure_future(_recording_watchdog())

async def _recording_watchdog():
    """録音中の監視タスク

    - voice_client が切断されたら自動再参加
    - リスナーが外れたら再アタッチ
    - 30秒ごとに現在のバッファをディスクにチェックポイント保存
      （プロセス強制終了時のデータ消失を防ぐ）
    - どの例外も握りつぶして必ずループを継続する
    """
    global voice_client
    disconnected_since = None
    last_checkpoint = time.time()
    CHECKPOINT_INTERVAL = 30  # 秒
    empty_polls = 0  # 対象VCが連続して無人だったポーリング回数

    while is_recording:
        try:
            await asyncio.sleep(5)
            if not is_recording:
                break

            # === 1) ボイス接続チェック ===
            connected = False
            try:
                connected = bool(voice_client and voice_client.is_connected())
            except Exception as e:
                print(f"⚠️ is_connected() チェック失敗: {e}", flush=True)
                connected = False

            if not connected:
                if disconnected_since is None:
                    disconnected_since = time.time()
                    print("⚠️ ボイス接続が切断されています。復旧を試みます...", flush=True)

                elapsed = time.time() - disconnected_since

                # 10秒以上切断継続でフル再参加
                if elapsed >= 10:
                    try:
                        guild = bot.get_guild(int(config['guild_id']))
                        voice_channel = discord.utils.get(guild.voice_channels, name=config['voice_channel_name']) if guild else None
                        if not voice_channel:
                            print("❌ 再参加先のボイスチャンネルが見つかりません", flush=True)
                            disconnected_since = time.time() - 5
                            continue

                        # 残留接続を全て破棄
                        if voice_client:
                            try:
                                await voice_client.disconnect(force=True)
                            except Exception:
                                pass
                        for vc in list(bot.voice_clients):
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass

                        print(f"🔄 {voice_channel.name} に再参加します...", flush=True)
                        voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient, reconnect=True, timeout=20.0)

                        if audio_sink:
                            audio_sink.vc = voice_client
                            voice_client.listen(audio_sink)
                            print("✅ ボイスチャンネル再参加＆録音再開しました", flush=True)
                        disconnected_since = None
                    except Exception as e:
                        print(f"⚠️ 再参加失敗: {e}（次のループで再試行）", flush=True)
                        import traceback
                        traceback.print_exc()
                        disconnected_since = time.time() - 5
            else:
                # 接続復活
                if disconnected_since is not None:
                    print("✅ ボイス接続が復旧しました", flush=True)
                    disconnected_since = None

                # リスナーが外れていれば再アタッチ
                try:
                    if not voice_client.is_listening() and audio_sink:
                        print("🔄 録音リスナーが外れています。再アタッチします...", flush=True)
                        audio_sink.vc = voice_client
                        voice_client.listen(audio_sink)
                        print("✅ 録音リスナーを再アタッチしました", flush=True)
                except Exception as e:
                    print(f"⚠️ リスナー再アタッチ失敗: {e}", flush=True)

                # === 退出取りこぼしの保険 ===
                # on_voice_state_update は voice_client の差し替え中に来た
                # 「最後の1人の退出」を取りこぼすことがある。接続復帰後に対象VCが
                # 連続して無人なら、ここで確実に会議を終了する。
                try:
                    ch = voice_client.channel if voice_client else None
                    non_bot = len([m for m in ch.members if not m.bot]) if ch else 1
                except Exception:
                    non_bot = 1  # 取得失敗時は終了させない（安全側）
                empty_polls, should_end = should_end_meeting(non_bot, empty_polls)
                if should_end:
                    print("⚠️ 対象VCに参加者がいません。会議を終了します"
                          "（退出取りこぼしの保険）", flush=True)
                    await stop_meeting()
                    break

            # === 2) 定期チェックポイント保存（プロセス死亡対策） ===
            # 増分追記なので O(新規) で軽く、別スレッドに逃がしてイベントループを
            # 一切止めない（旧実装は全バッファを毎回同期書き出ししていた）。
            now = time.time()
            if audio_sink and recording_dir and (now - last_checkpoint) >= CHECKPOINT_INTERVAL:
                try:
                    await asyncio.to_thread(audio_sink.flush_to_disk, recording_dir)
                    last_checkpoint = now
                except Exception as e:
                    print(f"⚠️ チェックポイント保存失敗: {e}", flush=True)
                    import traceback
                    traceback.print_exc()

        except Exception as e:
            # watchdogループ自体は絶対に死なせない
            print(f"⚠️ watchdog ループで例外: {e}", flush=True)
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)

async def stop_meeting():
    """会議終了＆録音停止"""
    global is_recording, voice_client, recording_dir, audio_sink

    if not is_recording:
        print("⚠️ 録音中ではありません")
        # 録音は終了済みだが、再接続でVCに残っている場合はクリーンアップ
        for vc in bot.voice_clients:
            try:
                await vc.disconnect(force=True)
                print("🧹 残留していたボイス接続を強制切断しました", flush=True)
            except Exception as e:
                print(f"⚠️ 強制切断エラー: {e}", flush=True)
        return

    print("⏹️ 録音を停止します")

    # 重要: stop_listening()の前にバッファの参照を保存
    temp_recording_dir = recording_dir
    temp_audio_sink = audio_sink
    temp_meeting_start_time = meeting_start_time
    meeting_end_time = datetime.now()

    # 少し待機してバッファを安定させる
    await asyncio.sleep(1)

    # 録音停止（この後バッファがクリアされる可能性がある）
    if voice_client and voice_client.is_listening():
        voice_client.stop_listening()

    is_recording = False

    # 音声データをファイルに保存（保存した参照を使用）。
    # 残バッファのflush＋WAV確定はブロッキングなので別スレッドで。
    if temp_recording_dir and temp_audio_sink:
        print("💾 音声ファイルを保存中...")
        await asyncio.to_thread(temp_audio_sink.finalize, temp_recording_dir)

    # 先にボイスチャンネルから切断（文字起こしは時間がかかるため）
    if voice_client:
        try:
            await voice_client.disconnect(force=True)
        except Exception as e:
            print(f"⚠️ 切断エラー: {e}", flush=True)
        voice_client = None
    # 念のためすべてのボイス接続を切断（再接続による残留を防止）
    for vc in bot.voice_clients:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

    # 次回録音のためにクリア
    audio_sink = None

    # 文字起こし実行（切断後にバックグラウンドで処理）
    if temp_recording_dir and os.path.exists(temp_recording_dir):
        wav_files = [f for f in os.listdir(temp_recording_dir) if f.endswith('.wav')]
        transcription_ok = False
        transcript = ""
        if wav_files:
            print("📝 文字起こしを開始します...")
            try:
                transcript = await transcribe_audio(temp_recording_dir)
            except Exception as e:
                # 想定外の文字起こし失敗のみ通知（通知先はDiscordのAI議事録ch）。
                # 無発話は例外でなく空文字で返るのでここには来ない
                print(f"❌ 文字起こしエラー: {e}", flush=True)
                import traceback
                traceback.print_exc()
                try:
                    await post_to_discord(
                        f"⚠️ 会議の文字起こしに失敗しました\n"
                        f"エラー: {e}\n録音ディレクトリ: {temp_recording_dir}")
                except Exception:
                    pass
            else:
                if transcript.strip():
                    transcription_ok = True
                else:
                    # 無発話（テスト入室・無音・極端に短い録音）は正常系。
                    # エラー通知も議事録生成もせず静かにスキップする
                    print("🔇 発話が検出されなかったため議事録はスキップします",
                          flush=True)
        else:
            print("⚠️ 録音データが空でした")

        # 議事録生成フラグファイルを作成（文字起こし成功時のみ）
        # 失敗時はSlack通知済み＆transcript.txtが無くリトライ対象にならないため作らない
        if transcription_ok:
            # 1. 先にフラグを作成 = リトライの種を必ず残す
            #    （この後のインライン生成が失敗してもリトライループ/手動で復旧できる）
            print("📋 議事録生成フラグを作成します...")
            flag_file = f"{temp_recording_dir}/pending_minutes.json"
            meeting_participants = list(temp_audio_sink.participants if temp_audio_sink else participants)
            with open(flag_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'recording_dir': temp_recording_dir,
                    'start_time': temp_meeting_start_time.isoformat(),
                    'end_time': meeting_end_time.isoformat(),
                    'participants': meeting_participants,
                    'created_at': datetime.now().isoformat()
                }, f, indent=2, ensure_ascii=False)

            # 2. インラインで議事録生成 → Discord投稿（自動完結）
            #    生成は runner/invoke_claude.py 経由のヘッドレス実行（v2 Phase 0）。
            #    2026-06-15 以降は月次 Agent SDK クレジットで賄われる想定。
            #    失敗時は pending_minutes.json が残り、Bot自身のリトライループ
            #    （_pending_minutes_loop）が再処理する。
            #    例外は握りつぶしてBotを落とさない。
            _inflight_minutes.add(temp_recording_dir)
            try:
                print("🤖 議事録を生成します...")
                # generate_minutes 内のブロッキング処理を別スレッドで実行し、
                # Discordのイベントループを止めない
                minutes = await asyncio.to_thread(
                    generate_minutes,
                    transcript,
                    temp_meeting_start_time,
                    meeting_end_time,
                    meeting_participants
                )
                print("📤 議事録をDiscordに投稿します...")
                await post_to_discord(minutes)
                # 投稿成功時のみフラグ削除（リトライループの二重投稿を防止）
                if os.path.exists(flag_file):
                    os.remove(flag_file)
                print("✅ 議事録の自動投稿が完了しました")
            except Exception as e:
                print(f"⚠️ 議事録の自動投稿に失敗しました（リトライループで再処理されます）: {e}", flush=True)
                import traceback
                traceback.print_exc()
            finally:
                _inflight_minutes.discard(temp_recording_dir)
    else:
        print("⚠️ 録音ディレクトリが見つかりませんでした")

@bot.command(name='start_meeting')
async def cmd_start_meeting(ctx):
    """手動で会議開始"""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        global voice_client
        voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        await ctx.send(f"✅ {channel.name} に参加しました")
        await start_recording()
        await ctx.send("🎙️ 録音を開始しました。話してください！")
    else:
        await ctx.send("❌ ボイスチャンネルに参加してからコマンドを実行してください")

@bot.command(name='stop_meeting')
async def cmd_stop_meeting(ctx):
    """手動で会議終了"""
    await ctx.send("⏹️ 会議を終了します...")
    await stop_meeting()
    await ctx.send("✅ 会議を終了しました")

@bot.command(name='ping')
async def cmd_ping(ctx):
    """疎通確認"""
    await ctx.send(f"🏓 pong! (遅延: {round(bot.latency * 1000)}ms)")

@bot.command(name='status')
async def cmd_status(ctx):
    """録音状態確認"""
    if is_recording:
        duration = (datetime.now() - meeting_start_time).total_seconds()
        chunks = sum(data['chunk_count'] for data in audio_sink.audio_buffers.values()) if audio_sink else 0
        await ctx.send(f"🎙️ 録音中: {duration:.1f}秒, {chunks}チャンク, {len(audio_sink.participants if audio_sink else [])}人参加")
    else:
        await ctx.send("⏸️ 録音していません")

if __name__ == '__main__':
    # ボイスパケットのINFOスパム(Received packet for unknown ssrc)を抑制。
    # これがstderr(bot.error.log)を肥大化させイベントループを詰まらせ、
    # ハートビート遅延→Discordセッション無効化→切断の原因になっていた。
    logging.getLogger('discord.ext.voice_recv').setLevel(logging.ERROR)
    bot.run(DISCORD_TOKEN, log_level=logging.WARNING)
