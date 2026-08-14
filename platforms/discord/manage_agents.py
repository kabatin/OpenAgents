#!/usr/bin/env python3
"""
Webhook人格の登録・一覧・退役CLI（spawn_agent の前身。エージェントv2 Phase 2）。

新しい役割を「設定の束」だけで産む:
  ./venv/bin/python manage_agents.py add \\
      --id keiri --name 経理係 --home-channel 123456789 \\
      --persona personas/keiri.md --avatar https://.../icon.png

一覧:   ./venv/bin/python manage_agents.py list
退役:   ./venv/bin/python manage_agents.py retire --id keiri

登録後、archivebot を再起動すると（launchctl kickstart）新人格が即日参加する。
"""

import argparse
import json
import os

from core import db
from platforms.discord import webhooks
from core import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = paths.DB_PATH
CONFIG_PATH = os.path.join(HERE, "config.json")


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _real_agents():
    """config.jsonの本物Bot（既存3体）を返す。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("agents", [])
    except OSError:
        return []


def _validate_add(args, existing):
    """登録前の検証。id/ホームch/名前の衝突となりすましを弾く（HIGH対応）。
    問題があれば理由文字列、無ければ None。"""
    reals = _real_agents()
    real_ids = {a["id"] for a in reals}
    real_homes = {int(a["home_channel_id"]) for a in reals}
    # id衝突（本物Bot・既存人格）→ ルールnamespace汚染・ルーティング乗っ取りを防ぐ
    if args.id in real_ids or any(a["id"] == args.id for a in existing):
        return f"id={args.id} は既存エージェントと衝突します"
    # ホームch: 本物Botのホーム（二重応答）・他人格のホーム（上書き）を禁止
    try:
        home = int(args.home_channel)
    except ValueError:
        return f"home-channel は数値のチャンネルIDで指定してください: {args.home_channel!r}"
    if home in real_homes:
        return "home-channel が本物Botのホームchと重複しています（二重応答の恐れ）"
    if any(int(a["home_channel_id"]) == home for a in existing):
        return "home-channel が既存のWebhook人格と重複しています"
    # 名前のなりすまし（本物Bot名・既存人格名と紛らわしい）
    taken = ({a.get("name", "").lower() for a in reals}
             | {a["name"].lower() for a in existing})
    return webhooks.validate_persona_name(args.name, existing_names=taken)


def cmd_add(args):
    persona = os.path.normpath(os.path.join(HERE, args.persona))
    # パストラバーサル防止: personaはリポジトリ配下に限定
    if not (persona + os.sep).startswith(HERE + os.sep):
        raise SystemExit(f"persona はリポジトリ配下を指定してください: {persona}")
    if not os.path.exists(persona):
        raise SystemExit(f"persona ファイルが見つかりません: {persona}")
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        existing = db.get_active_agents(conn)
    err = _validate_add(args, existing)
    if err:
        raise SystemExit(f"❌ 登録できません: {err}")
    skills = {s: True for s in (args.skill or [])}
    with db.connect(DB_PATH) as conn:
        db.add_agent(
            conn, id=args.id, kind="webhook", name=args.name,
            avatar_url=args.avatar, home_channel_id=int(args.home_channel),
            persona_file=os.path.relpath(persona, HERE),
            skills_json=json.dumps(skills, ensure_ascii=False),
            allowed_tools_json="[]",
            created_at=_now())
    print(f"✅ 登録: id={args.id} name={args.name} "
          f"home_channel={args.home_channel}")
    print("   archivebot を再起動すると参加します: "
          "launchctl kickstart -k gui/$(id -u)/com.discord.archivebot")


def cmd_list(args):
    with db.connect(DB_PATH) as conn:
        agents = db.get_active_agents(conn)
    if not agents:
        print("（Webhook人格は未登録）")
        return
    for a in agents:
        skills = list(json.loads(a["skills_json"] or "{}").keys())
        print(f"- {a['id']}: {a['name']} "
              f"(home={a['home_channel_id']}, skills={skills})")


def cmd_retire(args):
    with db.connect(DB_PATH) as conn:
        ok = db.retire_agent(conn, args.id)
    print("✅ 退役しました" if ok else "⚠️ 対象が見つかりません")


def main():
    ap = argparse.ArgumentParser(description="Webhook人格の管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="人格を登録")
    a.add_argument("--id", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--home-channel", required=True)
    a.add_argument("--persona", required=True, help="personas/*.md への相対パス")
    a.add_argument("--avatar", default=None,
                   help="アイコンURL or ローカルパス（任意）")
    a.add_argument("--skill", action="append",
                   help="付与スキル（例: --skill hire）。省略でチャット専用")
    a.set_defaults(func=cmd_add)

    lp = sub.add_parser("list", help="一覧")
    lp.set_defaults(func=cmd_list)

    r = sub.add_parser("retire", help="退役")
    r.add_argument("--id", required=True)
    r.set_defaults(func=cmd_retire)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
