#!/usr/bin/env python3
"""スーパーバイザ（core/supervisor.py・heartbeat.py・control.py）のテスト。

再起動の暴走は一番怖い挙動なので、実プロセスを起こさずに確かめられる
純粋関数として切り出してある。実際の起動・再起動も、すぐ終わる子プロセスを
使って1往復だけ確認する。

実行: python -m unittest core.test_supervisor -v
"""

import os
import shutil
import tempfile
import time
import unittest

from core import control
from core import heartbeat
from core import paths
from core import supervisor as sup


class BackoffTest(unittest.TestCase):
    """落ち続けるBOTを、CPUを焼かずに諦めずに再起動し続けること。"""

    def test_初回は待たない(self):
        self.assertEqual(sup.next_backoff(0), 0)

    def test_連続失敗で待ち時間が伸びる(self):
        waits = [sup.next_backoff(n) for n in range(1, 6)]
        self.assertEqual(waits[:4], list(sup.DEFAULT_BACKOFF))
        # 上限で頭打ちになる（無限に伸びると復旧しなくなる）
        self.assertEqual(waits[4], sup.DEFAULT_BACKOFF[-1])

    def test_待ち時間は単調増加(self):
        waits = [sup.next_backoff(n) for n in range(1, 10)]
        self.assertEqual(waits, sorted(waits))

    def test_安定したら連続失敗を打ち切る(self):
        self.assertTrue(sup.is_stable(sup.STABLE_AFTER_SEC))
        self.assertTrue(sup.is_stable(sup.STABLE_AFTER_SEC + 1))
        self.assertFalse(sup.is_stable(sup.STABLE_AFTER_SEC - 1))


class HangDetectionTest(unittest.TestCase):
    """プロセスは生きているのにループが止まった状態を捕まえること。"""

    def test_鮮度が閾値を超えたら再起動(self):
        self.assertTrue(sup.should_restart_for_hang(301, 300))

    def test_閾値ちょうどでは再起動しない(self):
        self.assertFalse(sup.should_restart_for_hang(300, 300))

    def test_まだ書かれていなければ再起動しない(self):
        # 起動直後を「無音」でハング扱いすると、再起動の無限ループになる
        self.assertFalse(sup.should_restart_for_hang(None, 300))

    def test_監視しない設定なら常に再起動しない(self):
        # 議事録BOTのように、数日無音が正常なものがある
        self.assertFalse(sup.should_restart_for_hang(99999, 0))
        self.assertFalse(sup.should_restart_for_hang(None, 0))


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = paths.HEARTBEAT_DIR
        paths.HEARTBEAT_DIR = os.path.join(self.tmp, "heartbeat")

    def tearDown(self):
        paths.HEARTBEAT_DIR = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_書いて読める(self):
        heartbeat.touch("x", now=1000)
        self.assertEqual(heartbeat.read("x"), 1000)

    def test_鮮度を秒で返す(self):
        heartbeat.touch("x", now=1000)
        self.assertEqual(heartbeat.age_sec("x", now=1030), 30)

    def test_未書き込みはNone(self):
        self.assertIsNone(heartbeat.read("nope"))
        self.assertIsNone(heartbeat.age_sec("nope"))

    def test_時計が巻き戻っても負にならない(self):
        heartbeat.touch("x", now=1000)
        self.assertEqual(heartbeat.age_sec("x", now=900), 0)

    def test_壊れた中身はNoneとして扱う(self):
        heartbeat.touch("x", now=1000)
        with open(heartbeat.path_for("x"), "w", encoding="utf-8") as f:
            f.write("こわれてる")
        self.assertIsNone(heartbeat.read("x"))

    def test_消せる(self):
        heartbeat.touch("x", now=1000)
        heartbeat.clear("x")
        self.assertIsNone(heartbeat.read("x"))
        heartbeat.clear("x")   # 二度目でも落ちない

    def test_書けない場所でも例外を投げない(self):
        # 監視のための処理が本体を殺すのは本末転倒。
        # 「作れない場所」はOSごとに違う（/proc/… は Windows だと C:\proc に
        # 作れてしまう）ので、**ファイルの下**というどのOSでも不可能な場所を使う
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        paths.HEARTBEAT_DIR = os.path.join(blocker, "heartbeat")
        self.assertFalse(heartbeat.touch("x"))


class LogRotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "bot.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, size):
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("x" * size)

    def test_小さいうちは退避しない(self):
        self._write(100)
        self.assertFalse(sup.rotate_log(self.log, max_bytes=1000))
        self.assertTrue(os.path.exists(self.log))

    def test_大きくなったら退避する(self):
        self._write(1000)
        self.assertTrue(sup.rotate_log(self.log, max_bytes=1000))
        self.assertTrue(os.path.exists(self.log + ".1"))
        self.assertFalse(os.path.exists(self.log))

    def test_世代がずれていく(self):
        for _ in range(3):
            self._write(1000)
            sup.rotate_log(self.log, max_bytes=1000, keep=3)
        self.assertTrue(os.path.exists(self.log + ".1"))
        self.assertTrue(os.path.exists(self.log + ".2"))
        self.assertTrue(os.path.exists(self.log + ".3"))

    def test_古すぎる世代は捨てる(self):
        for _ in range(5):
            self._write(1000)
            sup.rotate_log(self.log, max_bytes=1000, keep=2)
        self.assertFalse(os.path.exists(self.log + ".3"))

    def test_ファイルが無くても落ちない(self):
        self.assertFalse(sup.rotate_log(os.path.join(self.tmp, "none.log")))

    def test_判定は純粋関数(self):
        self.assertTrue(sup.rotate_needed(1000, 1000))
        self.assertFalse(sup.rotate_needed(999, 1000))


class PlanTest(unittest.TestCase):
    """設定から「動かすべきBOT」が正しく決まること。"""

    BASE = {"agents": [{"id": "a1"}]}

    def _ids(self, cfg):
        return {s.id: s.enabled for s in sup.plan_services(cfg)}

    def test_エージェントがいれば会話BOTが動く(self):
        self.assertTrue(self._ids(self.BASE)["archivebot"])

    def test_エージェントがいなければ動かさない(self):
        self.assertFalse(self._ids({"agents": []})["archivebot"])

    def test_開発BOTと議事録BOTは既定オフ(self):
        got = self._ids(self.BASE)
        self.assertFalse(got["devbot"])
        self.assertFalse(got["meetingbot"])

    def test_設定でオンにできる(self):
        cfg = {**self.BASE, "dev_bot": {"enabled": True},
               "meeting_bot": {"enabled": True}}
        got = self._ids(cfg)
        self.assertTrue(got["devbot"])
        self.assertTrue(got["meetingbot"])

    def test_議事録BOTは鮮度で判定しない(self):
        svc = {s.id: s for s in sup.plan_services(self.BASE)}["meetingbot"]
        self.assertEqual(svc.stale_after, 0)

    def test_会話BOTは鮮度で判定する(self):
        svc = {s.id: s for s in sup.plan_services(self.BASE)}["archivebot"]
        self.assertGreater(svc.stale_after, 0)

    def test_起動モジュールが指定されている(self):
        for svc in sup.plan_services(self.BASE):
            self.assertTrue(svc.module.startswith("platforms."))


class ChildEnvTest(unittest.TestCase):
    def test_リポジトリルートがimport経路に入る(self):
        env = sup.child_env()
        self.assertIn(paths.ROOT, env["PYTHONPATH"].split(os.pathsep))

    def test_既存のPYTHONPATHを消さない(self):
        saved = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = "/somewhere"
        try:
            self.assertIn("/somewhere",
                          sup.child_env()["PYTHONPATH"].split(os.pathsep))
        finally:
            if saved is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = saved


class RouteTest(unittest.TestCase):
    """操作用APIの振り分け。GETで状態が変わらないことが要点。"""

    def test_状態取得はGET(self):
        self.assertEqual(control.route("GET", "/status"), ("status", None))

    def test_再起動はPOSTのみ(self):
        self.assertEqual(control.route("POST", "/restart/devbot"),
                         ("restart", "devbot"))
        # GETで再起動できると、画像タグを踏ませるだけでBOTを落とせてしまう
        self.assertEqual(control.route("GET", "/restart/devbot"),
                         ("method_not_allowed", None))

    def test_開始と停止もPOSTのみ(self):
        for action in ("start", "stop"):
            self.assertEqual(control.route("POST", f"/{action}/archivebot"),
                             (action, "archivebot"))
            self.assertEqual(control.route("GET", f"/{action}/archivebot"),
                             ("method_not_allowed", None))

    def test_状態取得をPOSTでは受けない(self):
        self.assertEqual(control.route("POST", "/status"),
                         ("method_not_allowed", None))

    def test_クエリ文字列は無視する(self):
        self.assertEqual(control.route("GET", "/status?t=1"), ("status", None))

    def test_知らないパスは404(self):
        for path in ("/", "/nope", "/restart", "/restart/a/b"):
            kind, _ = control.route("POST", path)
            self.assertIn(kind, ("not_found", "method_not_allowed"), path)

    def test_外向きには待ち受けない(self):
        # ここを 0.0.0.0 にすると、同じLANの誰でもBOTを止められる
        self.assertEqual(control.HOST, "127.0.0.1")


class RunnerLifecycleTest(unittest.TestCase):
    """実際に子プロセスを起こして、起動・停止・再起動が回ること。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved_logs = paths.LOGS_DIR
        self.saved_hb = paths.HEARTBEAT_DIR
        paths.LOGS_DIR = os.path.join(self.tmp, "logs")
        paths.HEARTBEAT_DIR = os.path.join(self.tmp, "heartbeat")
        os.makedirs(paths.LOGS_DIR, exist_ok=True)
        os.makedirs(paths.HEARTBEAT_DIR, exist_ok=True)
        self.events = []

    def tearDown(self):
        paths.LOGS_DIR = self.saved_logs
        paths.HEARTBEAT_DIR = self.saved_hb
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, module, stale_after=0):
        svc = sup.ServiceDef("testsvc", "テスト", module,
                             stale_after=stale_after)
        return sup.ServiceRunner(
            svc, on_event=lambda *a: self.events.append(a))

    def _wait_for(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_起動して停止できる(self):
        # すぐには終わらないモジュール（標準ライブラリのHTTPサーバ）を使う
        runner = self._runner("http.server")
        runner.start()
        self.assertTrue(self._wait_for(lambda: runner.state == "running"),
                        f"起動しませんでした: {self.events}")
        self.assertIsNotNone(runner.status()["pid"])
        runner.stop()
        self.assertEqual(runner.state, "stopped")
        self.assertIsNone(runner.status()["pid"])

    def test_落ちたら自動で再起動する(self):
        # 即座に終了するモジュール＝落ち続けるBOTの模擬
        runner = self._runner("this_module_does_not_exist")
        runner.start()
        try:
            self.assertTrue(
                self._wait_for(lambda: runner.restarts >= 1, timeout=15),
                f"再起動しませんでした: {self.events}")
            # 待ち時間を挟むので、暴走的に再起動していないこと
            self.assertLess(runner.restarts, 5)
        finally:
            runner.stop()

    def test_ログが書かれる(self):
        runner = self._runner("this_module_does_not_exist")
        runner.start()
        # ファイルが「できた」だけでは中身がまだ空のことがある（書き込みは非同期）
        written = self._wait_for(
            lambda: os.path.exists(runner.log_path)
            and os.path.getsize(runner.log_path) > 0, timeout=15)
        runner.stop()
        self.assertTrue(written, "ログが書かれませんでした")
        with open(runner.log_path, encoding="utf-8") as f:
            self.assertIn("No module named", f.read())

    def test_人が頼んだ再起動は失敗として数えない(self):
        runner = self._runner("http.server")
        runner.start()
        self.assertTrue(self._wait_for(lambda: runner.state == "running"))
        runner.restart()
        try:
            self.assertTrue(
                self._wait_for(lambda: runner.restarts >= 1 and
                               runner.state == "running", timeout=15),
                f"再起動後に立ち上がりませんでした: {self.events}")
            self.assertEqual(runner.failures, 0)
        finally:
            runner.stop()

    def test_無効なBOTは起動しない(self):
        svc = sup.ServiceDef("off", "オフ", "http.server", enabled=False)
        supervisor = sup.Supervisor.__new__(sup.Supervisor)
        supervisor.runners = {"off": sup.ServiceRunner(svc)}
        supervisor.start_all()
        try:
            self.assertEqual(supervisor.runners["off"].state, "disabled")
            with self.assertRaises(ValueError):
                supervisor.restart("off")
        finally:
            supervisor.stop_all()

    def test_知らないBOTの再起動はKeyError(self):
        supervisor = sup.Supervisor.__new__(sup.Supervisor)
        supervisor.runners = {}
        supervisor.refresh = lambda: None
        with self.assertRaises(KeyError):
            supervisor.restart("nope")


class RefreshTest(unittest.TestCase):
    """設定変更にスーパーバイザが追随できること。

    実際に起きた不具合: 設定を起動時に1回しか読まなかったため、
    セットアップ完了後もウィザードから会話BOTを起動できなかった
    （空の設定で計画された「無効」が最後まで残っていた）。
    """

    def _supervisor(self, cfg):
        return sup.Supervisor(cfg, on_event=lambda *a: None)

    def test_設定でBOTを有効にしたら起動要求が通る(self):
        supervisor = self._supervisor({"agents": []})
        self.assertFalse(
            supervisor.runners["archivebot"].service.enabled)
        # 利用者がウィザードでエージェントを登録した、という状況
        supervisor.refresh = None   # 本物の refresh は config.load() を読む
        import core.config as app_config
        saved = app_config.load
        app_config.load = lambda *a, **k: {"agents": [{"id": "a1"}]}
        try:
            sup.Supervisor.refresh(supervisor)
            self.assertTrue(
                supervisor.runners["archivebot"].service.enabled)
        finally:
            app_config.load = saved

    def test_設定が壊れていても今の判断を保つ(self):
        supervisor = self._supervisor({"agents": [{"id": "a1"}]})
        import core.config as app_config
        saved = app_config.load
        def broken(*a, **k):
            raise app_config.ConfigError("壊れている")
        app_config.load = broken
        try:
            sup.Supervisor.refresh(supervisor)
            # 壊れた設定で「無効」に倒したりしない
            self.assertTrue(
                supervisor.runners["archivebot"].service.enabled)
        finally:
            app_config.load = saved


class RestartSemanticsTest(unittest.TestCase):
    """再起動要求まわりの取りこぼし。

    実際に見つけた不具合: バックオフ中に押された「再起動」の要求が
    消えずに残り、次に起きたばかりの子が約5秒後に殺されていた。
    """

    def _runner(self):
        svc = sup.ServiceDef("t", "テスト", "http.server")
        return sup.ServiceRunner(svc, on_event=lambda *a: None)

    def test_再起動要求はバックオフを打ち切る(self):
        runner = self._runner()
        runner._want_restart.set()
        began = time.time()
        stopped = runner._sleep(30)   # 本来は30秒待つところ
        self.assertFalse(stopped)
        self.assertLess(time.time() - began, 2)

    def test_停止要求は最優先(self):
        runner = self._runner()
        runner._stop.set()
        self.assertTrue(runner._sleep(30))

    def test_要求が無ければ満了まで待つ(self):
        runner = self._runner()
        began = time.time()
        self.assertFalse(runner._sleep(0.6))
        self.assertGreaterEqual(time.time() - began, 0.5)


if __name__ == "__main__":
    unittest.main()
