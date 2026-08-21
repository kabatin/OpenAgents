# Discord Meeting Bot

Discordサーバの会議を自動録音・文字起こしして議事録を生成するBot。
launchd `com.discord.meetingbot`（KeepAlive）で常駐。

## 動作フロー（実装準拠）

1. **自動参加:** 人間が「ラウンジ」ボイスチャンネルに入室すると自動で参加・録音開始
   （時刻スケジュールでの自動参加は未実装。運用は「会議時刻に人が入室する」駆動）
2. **録音:** 話者（Discordユーザー）ごとにOpus受信→DAVE復号→PCM蓄積。
   watchdogが切断復帰と30秒ごとのWAVチェックポイントを担当
3. **自動終了:** Bot以外の参加者が全員退出したら録音終了
4. **文字起こし:** faster-whisper（`small` / CPU / int8 / ja）で話者別に文字起こし→時系列マージ
5. **議事録生成:** `claude` CLI（headless）で生成。失敗時は `pending_minutes.json` が残り、
   Bot自身が起動時・定期リトライで再生成する
6. **投稿:** Discord Webhookへ投稿（2000字分割）。無発話（無音・テスト入室）は静かにスキップ。
   想定外の文字起こし失敗時のみ、同じDiscord Webhook（AI議事録ch）へエラー通知

手動制御: `!start_meeting` / `!stop_meeting`

## セットアップ

```bash
cd platforms/discord/meeting
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg
```

### 設定

- **シークレット（トークン・Webhook URL）は環境変数で渡す**（launchd plistの
  `EnvironmentVariables` に設定。config.jsonへの平文記載は廃止）:
  - `DISCORD_MEETINGBOT_TOKEN` — Discord Botトークン
  - `MEETINGBOT_WEBHOOK_URL` — 議事録投稿先のDiscord Webhook URL
- `config.json`（非シークレット設定のみ・git追跡外）:
  - `guild_id` / `voice_channel_id`
  - `user_mapping`（Discord表示名→メンションIDの対応表。議事録のTODO担当者に使用）
  - `minutes_model`（省略時 claude-sonnet-4-6）

## 運用

- 再起動: `launchctl kickstart -k gui/$(id -u)/com.discord.meetingbot`
- plist変更後: `launchctl bootout gui/$(id -u)/com.discord.meetingbot && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.discord.meetingbot.plist`
- ログ: `bot.log` / `bot.error.log`（`com.discord.botlogrotate` が日次で50MB超をローテート）
- 録音データ: `recordings/YYYYMMDD_HHMMSS/`（WAVは文字起こし後に削除、`transcript.txt` は恒久保存）

## トラブルシューティング

- **VCに参加できない:** Bot権限（Connect / Speak / Use Voice Activity）とサーバー招待を確認
- **文字起こしが動かない:** `venv/bin/python3 -c "from faster_whisper import WhisperModel"` で確認
- **議事録が投稿されない:** `pending_minutes.json` が残っていればリトライ対象。
  `bot.error.log` で `claude` CLIのエラーを確認
