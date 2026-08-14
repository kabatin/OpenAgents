#!/usr/bin/env bash
# macOS でダブルクリックして起動するための入口。
# ターミナルを開かずに始められるようにしてある。
cd "$(dirname "$0")"
exec python3 start.py
