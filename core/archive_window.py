#!/usr/bin/env python3
"""過去ログをどこから記録するか（設定 archive.since）。プラットフォーム非依存。

昔から運用されている大きなサーバーにセットアップしたとき、全履歴を無条件に
取り込むと数時間〜数日かかり、DBも膨らむ。セットアップで「取り込まない／
直近N日／すべて」を選んでもらい、その起点を archive.since に保存する。

- 未設定・空 = すべて（既存の利用者の挙動を変えない）
- 起点より前は取りに行かない。起動のたびの差分取得（停止中の取りこぼし）は続く
- 起点を前にずらすと、足りない古い分だけを次の起動で取りに行く
  （チャンネルごとに「どこまで遡ったか」を DB に覚えておく）
単体テスト: test_archive_window.py
"""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def parse_since(raw):
    """設定値を UTC の datetime にする。None/空 は None（＝すべて）。
    日付だけなら日本時間の0時、オフセットなしの日時は日本時間として読む。
    読めない値は ValueError（黙って「すべて」に倒すと巨大な取り込みが走る）。"""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(f"archive.since は日付の文字列で指定してください: {raw!r}")
    try:
        if len(raw) == 10:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
            dt = datetime.combine(d, time(0, 0), tzinfo=JST)
        else:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
    except ValueError as e:
        raise ValueError(
            f"archive.since の形式が読めません: {raw!r}"
            "（例: 2026-07-01 または 2026-07-01T09:30:00+09:00）") from e
    return dt.astimezone(timezone.utc)


def since_key(since):
    """DB に覚える「どこまで遡ったか」の表記。すべて = 空文字。"""
    return "" if since is None else since.astimezone(timezone.utc).isoformat()


def needs_older(coverage, since):
    """もっと古い分を取りに行く必要があるか（純粋関数）。
    coverage: そのチャンネルで遡り済みの起点（None=未記録、""=すべて）。"""
    if coverage is None:
        return True
    if coverage == "":
        return False                 # 既に全部ある
    if since is None:
        return True                  # 「すべて」に広げた
    return since < datetime.fromisoformat(coverage)


def from_config(cfg):
    """config 全体から起点を取り出す（不正な値は ValueError）。"""
    return parse_since(((cfg or {}).get("archive") or {}).get("since"))
