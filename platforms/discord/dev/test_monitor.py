#!/usr/bin/env python3
"""監視ハットの判定ロジックの単体テスト（Discord接続不要）。

実行: ./venv/bin/python -m unittest test_monitor -v
（venvは chatbot のものを流用する想定）
"""

import unittest

from platforms.discord.dev import monitor
from platforms.discord.dev.monitor import Signals, classify, should_notify, format_notice


class ClassifyTest(unittest.TestCase):
    """観測→健全性の写像。優先順位: 停止 > 接続切れ > 無音 > 正常。"""

    def test_process_down_is_down(self):
        h = classify(Signals(process_alive=False, discord_online=None,
                             log_age_sec=None, last_exit_status=15))
        self.assertEqual(h.status, monitor.DOWN)
        self.assertIn("15", h.detail)   # 最終exitを添える

    def test_down_takes_priority_over_everything(self):
        # プロセスが死んでいれば他の信号に関わらず DOWN
        h = classify(Signals(process_alive=False, discord_online=True,
                             log_age_sec=0.0))
        self.assertEqual(h.status, monitor.DOWN)

    def test_alive_but_offline_is_disconnected(self):
        h = classify(Signals(process_alive=True, discord_online=False,
                             log_age_sec=10.0))
        self.assertEqual(h.status, monitor.DISCONNECTED)

    def test_online_and_fresh_is_ok(self):
        h = classify(Signals(process_alive=True, discord_online=True,
                             log_age_sec=30.0))
        self.assertEqual(h.status, monitor.OK)

    def test_unknown_presence_does_not_alarm(self):
        # discord_online=None（未取得）なら接続切れを主張せず、ログが新しければ OK
        h = classify(Signals(process_alive=True, discord_online=None,
                             log_age_sec=5.0))
        self.assertEqual(h.status, monitor.OK)

    def test_stalled_only_when_very_quiet(self):
        # オンラインでもログが閾値超で無音なら STALLED（要確認）
        h = classify(Signals(process_alive=True, discord_online=True,
                             log_age_sec=4000.0), stall_after_sec=1800)
        self.assertEqual(h.status, monitor.STALLED)

    def test_boundary_at_stall_threshold_is_ok(self):
        # ちょうど閾値は OK（超えたら STALLED＝ >）
        h = classify(Signals(process_alive=True, discord_online=True,
                             log_age_sec=1800.0), stall_after_sec=1800)
        self.assertEqual(h.status, monitor.OK)

    def test_missing_log_never_stalls(self):
        # ログ未取得(None)は無音判定に使わない
        h = classify(Signals(process_alive=True, discord_online=True,
                             log_age_sec=None))
        self.assertEqual(h.status, monitor.OK)


class ShouldNotifyTest(unittest.TestCase):
    """状態遷移時のみ通知（正常時は黙る／初回は黙る）。"""

    def test_first_observation_is_silent(self):
        self.assertFalse(should_notify(None, monitor.OK))
        self.assertFalse(should_notify(None, monitor.DOWN))

    def test_same_status_is_silent(self):
        self.assertFalse(should_notify(monitor.OK, monitor.OK))
        self.assertFalse(should_notify(monitor.DOWN, monitor.DOWN))

    def test_degradation_notifies(self):
        self.assertTrue(should_notify(monitor.OK, monitor.DOWN))
        self.assertTrue(should_notify(monitor.OK, monitor.DISCONNECTED))

    def test_recovery_notifies(self):
        self.assertTrue(should_notify(monitor.DOWN, monitor.OK))
        self.assertTrue(should_notify(monitor.DISCONNECTED, monitor.OK))


class FormatNoticeTest(unittest.TestCase):
    def test_includes_name_and_detail(self):
        msg = format_notice("archivebot",
                            monitor.Health(monitor.DOWN, "プロセスが停止しています"))
        self.assertIn("archivebot", msg)
        self.assertIn("停止", msg)


class ConfirmTest(unittest.TestCase):
    """通知デバウンス: 瞬断の🟡/✅往復spamを2回連続の確認で抑える。"""

    def test_down_confirms_immediately(self):
        _c, _s, confirmed = monitor.confirm(monitor.OK, 5, monitor.DOWN)
        self.assertEqual(confirmed, monitor.DOWN)

    def test_single_flap_never_confirms(self):
        # OK運用中に1回だけ切断→次はOK: DISCONNECTEDは確定しない
        c, s, confirmed = monitor.confirm(None, 0, monitor.DISCONNECTED)
        self.assertIsNone(confirmed)
        c, s, confirmed = monitor.confirm(c, s, monitor.OK)
        self.assertIsNone(confirmed)          # OKもまだ1回目
        _c, _s, confirmed = monitor.confirm(c, s, monitor.OK)
        self.assertEqual(confirmed, monitor.OK)

    def test_two_consecutive_observations_confirm(self):
        c, s, confirmed = monitor.confirm(None, 0, monitor.DISCONNECTED)
        self.assertIsNone(confirmed)
        _c, _s, confirmed = monitor.confirm(c, s, monitor.DISCONNECTED)
        self.assertEqual(confirmed, monitor.DISCONNECTED)

    def test_confirmed_state_stays_confirmed_while_stable(self):
        c, s = monitor.OK, monitor.CONFIRM_AFTER
        _c, _s, confirmed = monitor.confirm(c, s, monitor.OK)
        self.assertEqual(confirmed, monitor.OK)   # 安定中は毎回確定（通知はshould_notifyが抑止）


if __name__ == "__main__":
    unittest.main()


class CanaryVerdictTest(unittest.TestCase):
    """デプロイ後カナリア（エラーログ急増検知）の判定。"""

    def test_watching_within_threshold(self):
        self.assertEqual(
            monitor.canary_verdict(1000, 1000 + 10_000, 3600), "watching")

    def test_alert_on_error_growth(self):
        self.assertEqual(
            monitor.canary_verdict(1000, 1000 + 60_000, 3600), "alert")

    def test_ok_after_window(self):
        self.assertEqual(
            monitor.canary_verdict(0, 10 ** 9, 25 * 3600), "ok")

    def test_log_rotation_shrink_is_not_alert(self):
        self.assertEqual(monitor.canary_verdict(50_000, 100, 3600), "watching")
