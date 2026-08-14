#!/usr/bin/env bash
# ログイン時に OpenAgents を自動起動する（macOS / launchd）。
#
#   ./autostart/install-macos.sh          登録する
#   ./autostart/install-macos.sh --remove 解除する
#
# OSに登録するのは run.py 1本だけです。個々のBOTの起動・再起動は
# run.py（スーパーバイザ）が面倒を見るので、BOTを増やしてもここは変わりません。
set -euo pipefail

LABEL="com.openagents.supervisor"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✅ 自動起動を解除しました"
  exit 0
fi

# 使う Python を決める（venv があれば優先）
if [ -x "$ROOT/venv/bin/python" ]; then
  PYTHON="$ROOT/venv/bin/python"
else
  PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
  echo "❌ python3 が見つかりません。先に python start.py を実行してください" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/state/logs"

cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/run.py</string>
  </array>

  <key>WorkingDirectory</key><string>$ROOT</string>

  <!-- ログイン時に起動し、落ちたら launchd が起こし直す -->
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <!-- launchd から起動すると PATH が最小限になる。
       claude / codex を見つけられるよう、よくある置き場を足しておく -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>

  <key>StandardOutPath</key><string>$ROOT/state/logs/supervisor.log</string>
  <key>StandardErrorPath</key><string>$ROOT/state/logs/supervisor.log</string>
</dict>
</plist>
PLIST_END

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"

echo "✅ 自動起動を登録しました"
echo "   ラベル : $LABEL"
echo "   Python : $PYTHON"
echo "   ログ   : $ROOT/state/logs/supervisor.log"
echo
echo "   今すぐ起動する : launchctl kickstart -k $DOMAIN/$LABEL"
echo "   状態を見る     : launchctl print $DOMAIN/$LABEL | head"
echo "   解除する       : ./autostart/install-macos.sh --remove"
