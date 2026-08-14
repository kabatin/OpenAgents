#!/usr/bin/env python3
"""
質問BOTの検索・回答をDiscord抜きで試すCLI。
使い方: ./venv/bin/python ask.py [--agent agent1|design|marketing] "納期はどうなってる？"
"""

import argparse
import os
import sys
import json

from core import paths
from core import search

DB_PATH = paths.DB_PATH
CONFIG_PATH = paths.CONFIG_PATH


def load_agent(config, agent_id):
    """config.jsonのagentsから指定IDを探してagent dictを返す（tokenは含めない）。
    見つからない場合、agent1だけは search.DEFAULT_AGENT にフォールバック。"""
    for a in config.get("agents", []):
        if a.get("id") == agent_id:
            return {
                "name": a["name"],
                "persona_files": [
                    os.path.normpath(os.path.join(HERE, p))
                    for p in a["persona_files"]
                ],
                "role": a.get("role", ""),
            }
    if agent_id == "agent1":
        return search.DEFAULT_AGENT
    sys.exit(f"config.json に agent '{agent_id}' が見つかりません")


def main():
    parser = argparse.ArgumentParser(description="質問BOTの単体テストCLI")
    parser.add_argument("--agent", default="agent1",
                        help="エージェントID (default: agent1)")
    parser.add_argument("question", nargs="+", help="質問文")
    args = parser.parse_args()
    question = " ".join(args.question)

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    agent = load_agent(config, args.agent)

    result = search.answer_question(DB_PATH, config["guild_id"], question,
                                    agent=agent)
    print(f"[agent] {agent['name']}")
    print(f"[keywords] {result['keywords']}")
    print(f"[hits] {result['hits']}")
    print("-" * 60)
    print(result["answer"])


if __name__ == "__main__":
    main()
