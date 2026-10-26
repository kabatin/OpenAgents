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

もう1つ、**Discordにつながっていること**も別物。ネット断のあと、プロセスも
イベントループも生きたまま、Discord への接続だけが戻らないことがある
（discord.py の再接続待ちが7分を超えて延びる）。生存証明は書かれ続けるので
ハング検知では拾えない。そこで接続がゼロになった時刻を `<id>.offline` に残し
（ConnectionTracker）、スーパーバイザがその長さを見る。

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
    for path in (path_for(service_id), offline_path(service_id)):
        try:
            os.remove(path)
        except OSError:
            pass


# --- Discordへの接続（オフライン固着の検知） --------------------------------


def offline_path(service_id):
    return path_for(service_id) + ".offline"


def mark_offline(service_id, *, now=None):
    """接続がゼロになった時刻を残す。既にあれば最初の時刻のまま（上書きしない）。
    失敗してもBOT本体は止めない。"""
    if offline_since(service_id) is not None:
        return True
    try:
        os.makedirs(paths.HEARTBEAT_DIR, exist_ok=True)
        tmp = offline_path(service_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(now if now is not None else time.time())))
        os.replace(tmp, offline_path(service_id))
        return True
    except OSError as e:
        print(f"接続状態の書き込みに失敗しました（{service_id}）: {e}")
        return False


def mark_online(service_id):
    """つながったので「切れた時刻」を消す。"""
    try:
        os.remove(offline_path(service_id))
    except OSError:
        pass


def offline_since(service_id):
    """接続がゼロになった時刻（epoch秒）。つながっていれば None。"""
    try:
        with open(offline_path(service_id), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def offline_age(service_id, *, now=None):
    """接続ゼロが何秒続いているか。つながっていれば None。"""
    since = offline_since(service_id)
    if since is None:
        return None
    return max(0, int(now if now is not None else time.time()) - since)


class ConnectionTracker:
    """1プロセス内のBotのつながり具合をまとめ、全員が切れている間だけ
    「接続ゼロ」を記録する（1体でもつながっていれば正常）。

    Discord のイベント（on_connect / on_resumed / on_ready と on_disconnect）
    から呼ぶ。起動直後はまだ誰もつながっていないので begin() で接続ゼロを記録する
    （起動したきり一度もつながれない、も同じ固着として扱う）。"""

    def __init__(self, service_id, *, clock=None):
        self.service_id = service_id
        self._clock = clock or time.time
        self._up = set()

    def begin(self):
        mark_online(self.service_id)          # 前のプロセスの残りを消してから
        mark_offline(self.service_id, now=self._clock())

    def connected(self, key):
        self._up.add(key)
        mark_online(self.service_id)

    def disconnected(self, key):
        self._up.discard(key)
        if not self._up:
            mark_offline(self.service_id, now=self._clock())
