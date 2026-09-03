#!/usr/bin/env python3
"""決定台帳の言い直し重複を掃除する。

同じ決定が語順違いで複数行になっていた（例: 同じ販売決定が6行）。
以後は decisions.save_decisions が保存時に弾くので、これは既存分の一回限りの
掃除ツール。DEDUP_DAYS 以内の先行 active 行と同一なら、後の行を duplicate にする。

使い方:
  ./venv/bin/python -m core.dedupe_decisions          # 何を重複とみなすか表示（変更なし）
  ./venv/bin/python -m core.dedupe_decisions --apply  # 実際に duplicate へ更新
"""

import sys

from core import db
from core import decisions
from core import paths
from core import reminders
from core import textsim


def find_duplicates(rows, window_days=decisions.DEDUP_DAYS):
    """[(重複行, 元の行)] を返す（純粋関数・古い順に走査）。"""
    kept, dups = [], []
    for r in rows:
        r_at = reminders.parse_dt(r["created_at"])
        recent = [k for k in kept
                  if (r_at - reminders.parse_dt(k["created_at"])).days
                  <= window_days]
        same = textsim.find_same(r["decision"], recent)
        if same is not None:
            dups.append((r, same))
        else:
            kept.append(r)
    return dups


def main():
    apply = "--apply" in sys.argv
    with db.connect(paths.DB_PATH) as conn:
        rows = db.recent_decisions(conn, "0000")   # active 全件・古い順
    dups = find_duplicates(rows)
    for r, orig in dups:
        print(f"#{r['id']} ≒ #{orig['id']}  {r['decision'][:60]}")
    print(f"\nactive {len(rows)}件のうち重複 {len(dups)}件")
    if not apply:
        print("（--apply で duplicate に更新）")
        return
    with db.connect(paths.DB_PATH) as conn:
        db.mark_decisions_duplicate(conn, [r["id"] for r, _ in dups])
    print("更新した")


if __name__ == "__main__":
    main()
