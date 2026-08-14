#!/usr/bin/env python3
"""BOTたちの親プロセス。起動・監視・再起動・ログをまとめて面倒を見る。

`python run.py` がこれを動かす。**OSごとの常駐の仕組み（launchd /
タスクスケジューラ）に頼るのは「ログイン時にこれ1本を起動する」ところまで**で、
BOT個々の面倒はここが見る。こうすると mac と Windows で挙動が揃うし、
利用者が覚える設定も1つで済む。

## 何をするか

- config を読んで、有効なBOTだけを子プロセスとして起動する
- 落ちたら指数バックオフで再起動する（即座に何度も再起動しない）
- 生存証明（heartbeat）が古くなったら、ハングとみなして再起動する
- 標準出力／標準エラーを state/logs/<id>.log にまとめ、大きくなったら退避する
- 127.0.0.1 の control API で、ダッシュボードから状態確認と再起動ができる

## 設計の理由

**判定ロジックは純粋関数に分けてある**（`next_backoff` / `should_restart` /
`rotate_needed`）。プロセスを実際に起動しないとテストできない作りにすると、
再起動の暴走のような一番怖い挙動を確かめられなくなるため。

単体テスト: python -m unittest core.test_supervisor -v
"""

import os
import subprocess
import sys
import threading
import time

from core import config as app_config
from core import heartbeat
from core import paths

#: 再起動の待ち時間（秒）。連続で落ちるほど長くする
DEFAULT_BACKOFF = (5, 15, 60, 300)
#: この時間動き続けたら「安定した」とみなしてバックオフを戻す
STABLE_AFTER_SEC = 120
#: ログをこの大きさで退避する
LOG_MAX_BYTES = 20 * 1024 * 1024
#: 退避しておく世代数
LOG_KEEP = 3
#: 停止を頼んでから強制終了するまで
TERM_GRACE_SEC = 10


class ServiceDef:
    """管理するBOT1つ分の定義。"""

    def __init__(self, sid, label, module, *, enabled=True,
                 stale_after=heartbeat.DEFAULT_STALE_SEC, note=""):
        self.id = sid
        self.label = label
        #: `python -m <module>` で起動する
        self.module = module
        self.enabled = enabled
        #: 生存証明がこの秒数より古ければハングとみなす（0で監視しない）
        self.stale_after = stale_after
        self.note = note


def plan_services(cfg):
    """設定から「動かすべきBOT」の一覧を作る（純粋関数）。

    ここに1件足すと、スーパーバイザもダッシュボードも自動的に面倒を見る。
    """
    agents = cfg.get("agents") or []
    dev = cfg.get("dev_bot") or {}
    meeting = cfg.get("meeting_bot") or {}
    return [
        ServiceDef(
            "archivebot", "会話エージェント", "platforms.discord.bot",
            enabled=bool(agents),
            note=(f"{len(agents)}体が1プロセスで動くため、"
                  "再起動は全員同時になります") if agents else
                 "エージェントが登録されていません",
        ),
        ServiceDef(
            "devbot", "開発BOT", "platforms.discord.dev.bot",
            enabled=bool(dev.get("enabled")),
            note="Discordから開発を指示できます（既定はオフ）",
        ),
        ServiceDef(
            "meetingbot", "議事録BOT", "platforms.discord.meeting.bot",
            enabled=bool(meeting.get("enabled")),
            # 会議が無い間はループが回らないので、鮮度では判断しない
            stale_after=0,
            note="会議が無い間は無音が正常です（既定はオフ）",
        ),
    ]


# --- 判定（純粋関数・テスト対象） -----------------------------------------


def next_backoff(consecutive_failures, backoff=DEFAULT_BACKOFF):
    """何秒待ってから再起動するか。連続失敗が増えるほど長くなる。"""
    if consecutive_failures <= 0:
        return 0
    idx = min(consecutive_failures - 1, len(backoff) - 1)
    return backoff[idx]


def is_stable(ran_for_sec, stable_after=STABLE_AFTER_SEC):
    """十分に動き続けたか（＝失敗の連続を打ち切ってよいか）。"""
    return ran_for_sec >= stable_after


def should_restart_for_hang(age_sec, stale_after):
    """生存証明の鮮度からハング再起動すべきか（純粋関数）。

    stale_after=0 は「鮮度では判断しない」。イベント駆動で数日無音が
    正常なBOT（議事録）を、無音というだけで殺さないため。
    """
    if not stale_after:
        return False
    return heartbeat.stale_from(age_sec, stale_after)


def rotate_needed(size_bytes, max_bytes=LOG_MAX_BYTES):
    """ログを退避すべきか（純粋関数）。"""
    return size_bytes >= max_bytes


# --- ログ ------------------------------------------------------------------


def rotate_log(path, *, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP):
    """大きくなったログを .1 .2 … へ退避する（冪等・失敗しても止めない）。

    過去に、ログが数百MBまで膨らんでイベントループを詰まらせた事故がある。
    放っておくと必ず起きるので、書き手側で面倒を見る。
    """
    try:
        if not os.path.exists(path):
            return False
        if not rotate_needed(os.path.getsize(path), max_bytes):
            return False
        for i in range(keep - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        return True
    except OSError as e:
        print(f"ログの退避に失敗しました（{path}）: {e}")
        return False


def child_env():
    """子プロセスに渡す環境。リポジトリルートを import 経路に入れる。"""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (paths.ROOT + os.pathsep + existing
                         if existing else paths.ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    # Windows の既定エンコーディングは cp932/cp1252 で、日本語の print が
    # UnicodeEncodeError で**BOTごと落ちる**。子は常に UTF-8 で書かせる
    # （親はログを UTF-8 で読む前提になっている）
    env["PYTHONUTF8"] = "1"
    return env


# --- 1つのBOTの面倒を見るスレッド -------------------------------------------


class ServiceRunner:
    """1つのBOTを起動し、落ちたら再起動し続ける。"""

    def __init__(self, service, *, python=None, on_event=None):
        self.service = service
        self.python = python or sys.executable
        self.on_event = on_event or (lambda *a, **k: None)
        self.proc = None
        self.failures = 0
        self.restarts = 0
        self.started_at = None
        self.last_exit = None
        self.state = "stopped"
        self._stop = threading.Event()
        self._want_restart = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # -- 外から呼ぶ操作 --
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"svc-{self.service.id}", daemon=True)
        self._thread.start()

    def stop(self, *, timeout=TERM_GRACE_SEC):
        self._stop.set()
        self._kill_child(timeout=timeout)
        heartbeat.clear(self.service.id)
        self.state = "stopped"

    def restart(self):
        """今動いている子を落とす。監視ループがすぐ次を起こす。"""
        self._want_restart.set()
        self._kill_child()

    def status(self):
        proc = self.proc
        age = heartbeat.age_sec(self.service.id)
        return {
            "id": self.service.id,
            "label": self.service.label,
            "enabled": self.service.enabled,
            "state": self.state,
            "pid": proc.pid if proc and proc.poll() is None else None,
            "restarts": self.restarts,
            "failures": self.failures,
            "uptimeSec": (int(time.time() - self.started_at)
                          if self.started_at and self.state == "running"
                          else None),
            "lastExit": self.last_exit,
            "heartbeatAgeSec": age,
            "staleAfterSec": self.service.stale_after,
            "note": self.service.note,
            "logPath": self.log_path,
        }

    @property
    def log_path(self):
        return os.path.join(paths.LOGS_DIR, f"{self.service.id}.log")

    # -- 内部 --
    def _kill_child(self, *, timeout=TERM_GRACE_SEC):
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 行儀よく終われないなら強制終了（ハング時はこちらになる）
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass

    def _spawn(self):
        paths.ensure_state_dirs()
        rotate_log(self.log_path)
        log = open(self.log_path, "a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                [self.python, "-m", self.service.module],
                cwd=paths.ROOT,
                env=child_env(),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        finally:
            # Popen が継承したので親側は閉じてよい
            log.close()
        return proc

    def _loop(self):
        while not self._stop.is_set():
            # これから起こす1回で「再起動して」の要求は満たされる。
            # 消さずに残すと、起きたばかりの子が約5秒後に殺される
            self._want_restart.clear()
            self.state = "starting"
            try:
                proc = self._spawn()
            except OSError as e:
                self.state = "error"
                self.last_exit = f"起動できませんでした: {e}"
                self.on_event(self.service.id, "spawn_failed", self.last_exit)
                self.failures += 1
                if self._sleep(next_backoff(self.failures)):
                    break
                continue

            with self._lock:
                self.proc = proc
            self.started_at = time.time()
            self.state = "running"
            self.on_event(self.service.id, "started", f"pid={proc.pid}")

            code = self._watch(proc)

            ran_for = time.time() - (self.started_at or time.time())
            self.last_exit = f"exit={code}"
            with self._lock:
                self.proc = None
            if self._stop.is_set():
                break

            if self._want_restart.is_set():
                self._want_restart.clear()
                self.failures = 0        # 人が頼んだ再起動は失敗ではない
                self.restarts += 1
                continue

            if is_stable(ran_for):
                self.failures = 0        # 十分動いてから落ちた＝連続失敗ではない
            self.failures += 1
            self.restarts += 1
            wait = next_backoff(self.failures)
            self.state = "restarting"
            self.on_event(self.service.id, "exited",
                          f"exit={code} / {wait}秒後に再起動します")
            if self._sleep(wait):
                break
        self.state = "stopped"

    def _watch(self, proc):
        """子の終了を待ちつつ、生存証明の鮮度も見る。"""
        while True:
            try:
                return proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if self._stop.is_set() or self._want_restart.is_set():
                self._kill_child()
                continue
            age = heartbeat.age_sec(self.service.id)
            if should_restart_for_hang(age, self.service.stale_after):
                self.on_event(
                    self.service.id, "hang",
                    f"生存証明が{age}秒前で止まっています。再起動します")
                # 「再起動して」の経路に乗せる（回数はループ側が1回だけ数える。
                # ハングは連続失敗ではないので、バックオフも掛けない）
                self._want_restart.set()
                self._kill_child()

    def _sleep(self, seconds):
        """待つ。停止なら True。再起動要求が来たら早めに切り上げる
        （「今すぐ再起動」を、最長300秒のバックオフの後ろに並ばせない）。"""
        deadline = time.time() + seconds
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return False
            if self._stop.wait(min(0.5, remain)):
                return True
            if self._want_restart.is_set():
                return False


class Supervisor:
    """全BOTをまとめて面倒を見る。"""

    def __init__(self, cfg=None, *, python=None, on_event=None):
        self.cfg = cfg if cfg is not None else app_config.load()
        self.runners = {}
        for svc in plan_services(self.cfg):
            self.runners[svc.id] = ServiceRunner(
                svc, python=python, on_event=on_event or self._log_event)

    @staticmethod
    def _log_event(sid, kind, detail):
        print(f"[supervisor] {sid}: {kind} {detail}", flush=True)

    def start_all(self):
        paths.ensure_state_dirs()
        for runner in self.runners.values():
            if runner.service.enabled:
                runner.start()
            else:
                runner.state = "disabled"

    def stop_all(self):
        for runner in self.runners.values():
            runner.stop()

    def status(self):
        return {"services": [r.status() for r in self.runners.values()]}

    def refresh(self):
        """設定を読み直し、各BOTの有効/無効を最新にする。

        設定の中身は各BOTが自分の起動時に読むのでここでは触らないが、
        **どのBOTを動かすか**だけは追随しないといけない。しないと、
        画面から有効にしたBOTを（run.py を丸ごと再起動するまで）
        永遠に起動できない。
        """
        try:
            cfg = app_config.load()
        except app_config.ConfigError:
            return   # 壊れた設定で上書きしない（今の判断を維持）
        self.cfg = cfg
        for svc in plan_services(cfg):
            runner = self.runners.get(svc.id)
            if runner is not None:
                runner.service.enabled = svc.enabled
                runner.service.note = svc.note
                runner.service.stale_after = svc.stale_after

    def restart(self, service_id):
        self.refresh()   # 「さっき画面で有効にしたBOT」を起こせるように
        runner = self.runners.get(service_id)
        if runner is None:
            raise KeyError(service_id)
        if not runner.service.enabled:
            raise ValueError(f"{runner.service.label} は無効になっています")
        if runner.state in ("stopped", "disabled"):
            runner.start()
        else:
            runner.restart()

    def start(self, service_id):
        self.refresh()
        runner = self.runners[service_id]
        if not runner.service.enabled:
            raise ValueError(f"{runner.service.label} は無効になっています")
        runner.start()

    def stop(self, service_id):
        self.runners[service_id].stop()
