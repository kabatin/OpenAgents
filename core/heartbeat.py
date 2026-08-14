#!/usr/bin/env python3
"""生存証明（heartbeat）の読み書き。

**プロセスが生きていること**と**仕事ができていること**は別物。
過去に、プロセスは残ったままイベントループが詰まって無反応になる障害が
何度かあった。プロセスの生存だけを見ていると、この状態を「正常」と誤判定する。

そこで各BOTは、自分の主要ループが1周するたびにこのファイルへ時刻を書く。
スーパーバイザはその**鮮度**を見て、古ければ「ハングしている」と判断して
再起動する。

    core.heartbeat.touch("archivebot")     # BOT側: ループのたびに呼ぶ
    core.heartbeat.age_sec("archivebot")   # 監視側: 何秒前の生存証明か

単体テスト: python -m unittest core.test_supervisor -v
"""

import os
import time

from core import paths

#: これを超えたら「ループが止まっている」とみなす既定値（秒）
DEFAULT_STALE_SEC = 300


def path_for(service_id):
    return os.path.join(paths.HEARTBEAT_DIR, service_id)


def touch(service_id, *, now=None):
    """生存証明を更新する。失敗してもBOT本体は止めない。

    書き込みに失敗する（ディスク満杯・権限）状況で例外を投げると、
    「監視のための処理が本体を殺す」という本末転倒になる。
    """
    try:
        os.makedirs(paths.HEARTBEAT_DIR, exist_ok=True)
        stamp = str(int(now if now is not None else time.time()))
        tmp = path_for(service_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(stamp)
        os.replace(tmp, path_for(service_id))
        return True
    except OSError as e:
        print(f"heartbeat の書き込みに失敗しました（{service_id}）: {e}")
        return False


def read(service_id):
    """最後に書かれた時刻（epoch秒）。無ければ None。"""
    try:
        with open(path_for(service_id), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def age_sec(service_id, *, now=None):
    """何秒前の生存証明か。まだ書かれていなければ None。"""
    stamp = read(service_id)
    if stamp is None:
        return None
    return max(0, int(now if now is not None else time.time()) - stamp)


def is_stale(service_id, *, stale_after=DEFAULT_STALE_SEC, now=None):
    """ハングと判断してよいか（純粋な判定は stale_from を使う）。"""
    return stale_from(age_sec(service_id, now=now), stale_after)


def stale_from(age, stale_after=DEFAULT_STALE_SEC):
    """鮮度からハング判定する（純粋関数・テスト対象）。

    まだ1度も書かれていない（age が None）場合は **False**。
    起動直後をハング扱いにすると、再起動の無限ループになる。
    """
    if age is None:
        return False
    return age > stale_after


def clear(service_id):
    """生存証明を消す（停止時に呼ぶ。古い値で誤判定させない）。"""
    try:
        os.remove(path_for(service_id))
    except OSError:
        pass
