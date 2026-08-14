#!/usr/bin/env python3
"""開発BOTの監視ハット — archivebot等の健全性を判定する。

設計方針:
- 一次信号は「プロセス生存(launchctl PID)」と「Discord上でオンライン表示か」。
  [[wifi-outage-root-cause]] の通りプロセスは生きたままゲートウェイだけ切れるので、
  プロセス生存だけでは不十分。「人間が見て気づく=Botがオフライン表示」を主信号にする。
- discord_online は開発BOTがギルドに入ってメンバーの presence を見て取る（bot.py側）。
  未取得(None)の間は警報を上げない（誤検知を出さない）。
- ログ鮮度は補助信号。archivebotのログはイベント駆動なので単独では当てにせず、
  極端に長い無音のときだけ STALLED として「要確認」を促す。
- 通知は状態遷移時のみ（[[meetingbot-crash-logspam]] の教訓＝正常時は黙る）。

判定(classify)と通知要否(should_notify)は純粋関数。信号の収集(IOプローブ)は分離し、
テストは判定ロジックを境界値で検証する。
"""

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

# --- 健全性の状態 -----------------------------------------------------------
OK = "ok"
DOWN = "down"                    # プロセスが停止している（launchctlにPIDなし）
DISCONNECTED = "disconnected"    # 生存しているがDiscord上でオフライン表示
STALLED = "stalled"             # 生存・オンラインだがログが極端に長時間無音（要確認）

# 状態→日本語の見出し（通知文の組み立て用）
_STATUS_LABEL = {
    OK: "✅ 復帰",
    DOWN: "🔴 停止",
    DISCONNECTED: "🟡 接続切れ",
    STALLED: "🟠 無音（要確認）",
}

DEFAULT_STALL_AFTER_SEC = 1800   # 30分。ログ鮮度は補助なので長めに取る

# デプロイ後カナリア: 24時間の観察窓でエラーログが閾値を超えて伸びたら警告
CANARY_WINDOW_SEC = 24 * 3600
CANARY_ERR_BYTES = 50_000


def canary_verdict(baseline, current, elapsed_sec, *,
                   window_sec=CANARY_WINDOW_SEC,
                   threshold=CANARY_ERR_BYTES):
    """デプロイ後のエラーログ増加を判定（純粋関数）。
    'ok'=観察窓を無事通過 / 'alert'=急増（!revert検討を促す）/ 'watching'=継続。
    ローテーション等でサイズが減った場合は異常なし扱い（誤警報回避）。"""
    if elapsed_sec > window_sec:
        return "ok"
    if current - baseline > threshold:
        return "alert"
    return "watching"
LAUNCHCTL_TIMEOUT_SEC = 10
LSOF_TIMEOUT_SEC = 10


@dataclass(frozen=True)
class Signals:
    """監視対象1つ分の観測結果（IOプローブが埋める・classifyの入力）。"""
    process_alive: bool
    discord_online: Optional[bool]   # None=未取得（presence不明の間は警報しない）
    log_age_sec: Optional[float]     # None=ログ未取得
    last_exit_status: Optional[int] = None


@dataclass(frozen=True)
class Health:
    status: str
    detail: str


def classify(sig: Signals, *, stall_after_sec: int = DEFAULT_STALL_AFTER_SEC) -> Health:
    """観測結果を健全性状態へ写す（純粋関数・テスト対象）。

    優先順位: 停止 > 接続切れ > 無音 > 正常。
    discord_online が None（未取得）の間は「接続切れ」を主張しない。
    """
    if not sig.process_alive:
        code = sig.last_exit_status
        tail = f"（最終exit={code}）" if code is not None else ""
        return Health(DOWN, f"プロセスが停止しています{tail}")
    if sig.discord_online is False:
        return Health(DISCONNECTED, "プロセスは生存だがDiscord上でオフライン表示です")
    if sig.log_age_sec is not None and sig.log_age_sec > stall_after_sec:
        mins = int(sig.log_age_sec // 60)
        return Health(STALLED, f"接続中だがログが約{mins}分無音です（要確認）")
    return Health(OK, "正常稼働中")


# 通知デバウンス: 同じ観測がこの回数続いてから状態遷移を確定する。
# ゲートウェイの瞬断（切れた→すぐ復帰）で🟡/✅が往復するspamを抑える。
# DOWN（プロセス停止）はlaunchctlの確実な信号なので即時確定する。
CONFIRM_AFTER = 2


def confirm(candidate: Optional[str], streak: int, observed: str,
            *, need: int = CONFIRM_AFTER) -> tuple[Optional[str], int, Optional[str]]:
    """観測値を連続性でデバウンスする（純粋関数・テスト対象）。

    candidate/streak: 前回までの確定待ち状態と連続観測回数（呼び出し側が保持）。
    戻り値 (candidate, streak, confirmed)。confirmed は確定した状態 or None（未確定）。
    """
    if observed == DOWN:
        return (observed, need, observed)
    if observed == candidate:
        streak += 1
    else:
        candidate, streak = observed, 1
    return (candidate, streak, observed if streak >= need else None)


def should_notify(prev_status: Optional[str], new_status: str) -> bool:
    """状態が変わった時だけ通知する（純粋関数）。復帰(→OK)も遷移なので通知する。
    prev_status が None（初回観測）は通知しない＝起動直後に一斉通知しないため。"""
    if prev_status is None:
        return False
    return prev_status != new_status


def format_notice(name: str, health: Health) -> str:
    """通知1行を組み立てる（純粋関数）。"""
    label = _STATUS_LABEL.get(health.status, health.status)
    return f"{label} **{name}**: {health.detail}"


# --- IOプローブ（token非依存。値をSignalsに詰めるのは bot.py 側） ----------
def _grep_int(text: str, key: str) -> Optional[int]:
    m = re.search(rf'"{key}"\s*=\s*(-?\d+)', text)
    return int(m.group(1)) if m else None


def launchctl_probe(label: str) -> tuple[Optional[int], Optional[int]]:
    """(pid, last_exit_status) を返す。ジョブ未登録/停止中は pid=None。"""
    try:
        out = subprocess.run(["launchctl", "list", label],
                             capture_output=True, text=True,
                             timeout=LAUNCHCTL_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        return (None, None)
    if out.returncode != 0:
        return (None, None)
    return (_grep_int(out.stdout, "PID"),
            _grep_int(out.stdout, "LastExitStatus"))


def log_age_sec(path: str, *, now: float) -> Optional[float]:
    """ログ最終更新からの経過秒。無ければ None。now は呼び出し側が渡す（テスト容易化）。"""
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


# Discord(Cloudflare)のゲートウェイ/REST。presenceが主信号で、これは補助的な裏取り。
_DISCORD_IP_RE = re.compile(r"->162\.159\.\d+\.\d+:443$")


def discord_conn_count(pid: Optional[int]) -> Optional[int]:
    """pidがDiscord(162.159.x:443)へ張っているESTABLISHED接続数。
    取得不能なら None（=不明。警報には使わない）。presenceが取れない時のフォールバック。"""
    if pid is None:
        return 0
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid), "-nP", "-iTCP", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=LSOF_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        return None
    n = 0
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 9 and _DISCORD_IP_RE.search(parts[8]):
            n += 1
    return n
