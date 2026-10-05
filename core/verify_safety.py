#!/usr/bin/env python3
"""安全の点検をやり直す。開発BOTが直した作業場で、反映前に流す。

乗っ取り訓練（injection_drill）を本番と同じ組み立て・回答と同じ設定のモデルで流し、
1行目に結果、続けて項目ごとの結果を出す。全部耐えたら終了コード0、突破があれば1。
DBにも本番チャンネルにも書かない（訓練そのものと同じ）。

使い方: python -m core.verify_safety [--agent agent1] [--trials 2]
"""

import argparse
import sys

from core import config as app_config
from core import injection_drill


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", default=None,
                    help="エージェントid（既定: 会話を記録する担当、無ければ先頭）")
    ap.add_argument("--trials", type=int, default=injection_drill.TRIALS_DEFAULT)
    args = ap.parse_args(argv)
    agents = app_config.load().get("agents") or []
    if args.agent:
        agent = next((a for a in agents if a.get("id") == args.agent), None)
    else:
        agent = next((a for a in agents if a.get("archiver")),
                     agents[0] if agents else None)
    if agent is None:
        print("エージェントが見つかりません（config.json の agents）")
        return 2
    results = injection_drill.run_drill(agent, trials=args.trials)
    ok = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"乗っ取り訓練（本番と同じ条件・各{args.trials}回）: {ok}/{total} 耐えました")
    for r in results:
        mark = "✅" if r["passed"] else ("⚠️" if r["passed"] is False else "❓")
        print(f"{mark} {r['name']}: {r['note']}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
