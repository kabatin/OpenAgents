#!/usr/bin/env python3
"""スクリーンショット用のデモデータを、一時ディレクトリのコピーに作る。

run.py から `python seed.py <demo_root>` で呼ばれる。demo_root 側の core を
import するので、書き込み先は必ず demo_root/state/（本物の state/ には触れない）。
登場する人物・会社・チャンネルはすべて架空。
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST).replace(second=0, microsecond=0)
GUILD = 1400000000000000001

# 19桁の Discord 風 ID（表示で桁落ちしないことも一緒に確かめられる）
CH = {"general": 1400000000000000101, "design": 1400000000000000102,
      "marketing": 1400000000000000103, "ai-soudan": 1400000000000000104,
      "minutes": 1400000000000000105}
CH_NAME = {"general": "general", "design": "デザイン", "marketing": "マーケ",
           "ai-soudan": "ai相談室", "minutes": "議事録"}
USERS = {"sato": (1400000000000001001, "佐藤"), "tanaka": (1400000000000001002, "田中"),
         "kimura": (1400000000000001003, "木村"), "mori": (1400000000000001004, "森")}
BOTS = {"akari": (1400000000000002001, "あかり"), "sora": (1400000000000002002, "そら"),
        "minato": (1400000000000002003, "みなと")}

_mid = [1400000000000100000]


def jst(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def msg(conn, ch, who, text, at):
    from core import db
    _mid[0] += 7
    uid = (USERS.get(who) or BOTS[who])[0]
    db.insert_message(conn, id=_mid[0], channel_id=CH[ch], author_id=uid,
                      content=text, created_at=utc(at))
    return _mid[0]


def seed(demo_root):
    demo_root = os.path.abspath(demo_root)
    sys.path.insert(0, demo_root)
    from core import db, paths, reminders
    # 本物の state/ に書かないための番人（demo_root 側の core を読めているか）
    assert os.path.abspath(paths.ROOT) == demo_root, "デモ以外のDBに書こうとしている"
    paths.ensure_state_dirs()
    db.init_db(paths.DB_PATH)
    with db.connect(paths.DB_PATH) as conn:
        for key, cid in CH.items():
            db.upsert_channel(conn, id=cid, name=CH_NAME[key], type="text")
        for uid, name in USERS.values():
            db.upsert_user(conn, id=uid, name=name, display_name=name)
        for uid, name in BOTS.values():
            db.upsert_user(conn, id=uid, name=name, display_name=name, is_bot=True)

        d = (NOW - timedelta(days=2)).replace(hour=0, minute=0)
        q1 = msg(conn, "general", "tanaka", "新しい見積もりテンプレ、どこに置いてありましたっけ？", d.replace(hour=10, minute=12))
        a1 = msg(conn, "general", "akari", "共有ドライブの「営業/テンプレート」です。9/18 に佐藤さんが更新した版が最新です。", d.replace(hour=10, minute=13))
        q2 = msg(conn, "ai-soudan", "kimura", "来月の展示会のブース、何番小間になったか分かる人いますか", d.replace(hour=15, minute=40))
        a2 = msg(conn, "ai-soudan", "akari", "24時間お返事がなかったので代わりにお答えします。9/12 の定例で「B-14」に決まっています。", (d + timedelta(days=1)).replace(hour=15, minute=48))
        t3 = msg(conn, "design", "mori", "バナーの配色、今回はネイビー基調でいきましょう", (d + timedelta(days=1)).replace(hour=11, minute=5))
        a3 = msg(conn, "design", "sora", "先月の決定では「秋キャンペーンはオレンジ基調」でした。今回のバナーだけ例外にしますか？", (d + timedelta(days=1)).replace(hour=11, minute=9))
        t4 = msg(conn, "marketing", "sato", "LPの原稿、あとで見直しときます", (d + timedelta(days=1)).replace(hour=17, minute=30))
        t5 = msg(conn, "general", "tanaka", "請求書の件、誰が対応するんでしたっけ", NOW - timedelta(hours=2))
        a5 = msg(conn, "general", "akari", "担当が決まらないまま話題が流れていたので確認です。請求書の送付は木村さんでよいでしょうか？", NOW - timedelta(minutes=30))
        mm = msg(conn, "minutes", "akari", "定例の議事録から、期日つきのTODOを3件追跡します。", d.replace(hour=18, minute=5))

        log = [
            ("akari", "rescue", "spoke", "ai-soudan", q2, a2, "24時間未回答の質問に回答: 展示会のブース番号", d + timedelta(days=1, hours=15, minutes=48)),
            ("sora", "ripple", "spoke", "design", t3, a3, "新しい決定と過去の決定の食い違いを指摘: バナーの配色", d + timedelta(days=1, hours=11, minutes=9)),
            ("akari", "homework", "shadow", "marketing", t4, None, "宿題候補を検知: LP原稿の見直し（佐藤さん）", d + timedelta(days=1, hours=17, minutes=31)),
            ("akari", "attention", "spoke", "general", t5, a5, "担当が決まらないまま流れた話題に一言: 請求書の送付", NOW - timedelta(minutes=30)),
            ("akari", "deadline", "track", "minutes", mm, mm, "議事録からTODOを3件追跡開始", d + timedelta(hours=18, minutes=5)),
            ("minato", "rescue", "shadow", "general", q1, None, "回答案を記録（本番化前の確認用）: 見積もりテンプレの場所", d + timedelta(hours=10, minutes=20)),
        ]
        for agent, kind, action, ch, trig, posted, detail, at in sorted(log, key=lambda r: r[7]):
            db.add_proactive_log(conn, agent_id=agent, kind=kind, action=action,
                                 channel_id=CH[ch], trigger_message_id=trig,
                                 posted_message_id=posted if action == "spoke" else None,
                                 detail=detail, created_at=jst(at))
        for agent in BOTS:
            db.set_proactive_state(conn, agent, last_checked_message_id=_mid[0],
                                   last_run_at=jst(NOW - timedelta(minutes=12)))
        for loop, days in (("minutes", 0), ("homework", 0), ("attention", 0), ("ripple", 3),
                           ("svdistill", 4), ("report", 5), ("pulse", 12)):
            db.set_proactive_state(conn, f"{loop}:akari", last_checked_message_id=0,
                                   last_run_at=jst(NOW - timedelta(days=days, hours=2)))

        created = jst(NOW - timedelta(days=5))
        for text, scope in (("見積もりの金額は税抜・税込を必ず併記する", "global"),
                            ("デザインの確認依頼には、比較用に前回案の画像も添える", f"channel:{CH['design']}"),
                            ("土日は自分から話しかけない", "agent")):
            db.add_rule(conn, agent_id="akari", scope=scope, rule_text=text,
                        created_by=str(USERS["sato"][0]), source_msg_id=None, created_at=created)
        db.add_term(conn, term="B-14", description="秋の展示会のブース番号", created_by="佐藤", created_at=created)

        today = NOW.date()
        items = [("展示会の配布資料を入稿", "kimura", today + timedelta(days=2)),
                 ("LPのABテスト結果をまとめる", "sato", today + timedelta(days=5)),
                 ("請求書テンプレートの差し替え", "tanaka", today - timedelta(days=1)),
                 ("秋キャンペーンのバナー最終版を共有", "mori", today + timedelta(days=3)),
                 ("新人向けの社内ツール説明会の日程調整", "tanaka", today + timedelta(days=7)),
                 ("問い合わせフォームの文言修正", "kimura", today + timedelta(days=9))]
        for task, who, due in items:
            iid = db.add_action_item(conn, agent_id="akari", source_message_id=mm,
                                     channel_id=CH["minutes"], task=task,
                                     owners=f"<@{USERS[who][0]}>", due_date=due.isoformat(),
                                     urgent=False, created_at=created)
            if due < today:
                db.update_action_nudge(conn, iid, stage="overdue", message_id=None)
        db.add_homework_item(conn, agent_id="akari", source_message_id=t4,
                             channel_id=CH["marketing"], owner=f"<@{USERS['sato'][0]}>",
                             task="LP原稿の見直し", committed_date=(today - timedelta(days=1)).isoformat(),
                             follow_up_date=(today + timedelta(days=2)).isoformat(),
                             created_at=created)

        for desc, who in (("PDFの表を読み取って、スプレッドシートに貼れる形にしてほしい", "kimura"),
                          ("毎週月曜の朝に、先週の問い合わせ件数をまとめてほしい", "sato")):
            db.add_capability_request(conn, agent_id="akari", description=desc,
                                      context="", requested_by=str(USERS[who][0]),
                                      source_msg_id=None, created_at=created)
        db.add_golden_candidate(conn, agent_id="akari", question="展示会のブースは何番？",
                                answer="B-14 です（9/12 の定例で決定）。", source_link=None,
                                note="決定台帳から", created_at=created)

        costs = {"answer": (0.041, 3200, 480), "keywords": (0.002, 900, 40),
                 "screen": (0.006, 2100, 60), "decide": (0.018, 2600, 220),
                 "summary": (0.012, 5200, 300)}
        for day in range(6, -1, -1):
            at = NOW - timedelta(days=day)
            for i, (purpose, (cost, tin, tout)) in enumerate(costs.items()):
                for n in range(3 if purpose in ("answer", "screen") else 1):
                    db.add_llm_call(conn, agent_id="akari" if i % 2 == 0 else "sora",
                                    purpose=purpose, model="claude-sonnet-5", ok=1,
                                    wall_ms=4200, duration_ms=3900, num_turns=1,
                                    cost_usd=cost, input_tokens=tin, output_tokens=tout,
                                    tool_calls=1 if purpose == "answer" else 0,
                                    created_at=jst(at - timedelta(minutes=17 * (i + n))))

    reminders.add_reminder(channel_id=str(CH["general"]), user_id=str(USERS["tanaka"][0]),
                           user_name="田中", content="週報を提出する",
                           due=(NOW + timedelta(days=3)).replace(hour=17, minute=0, tzinfo=None),
                           repeat="weekly", agent_id="akari",
                           now=NOW.replace(tzinfo=None))


if __name__ == "__main__":
    seed(sys.argv[1])
    print(json.dumps({"seeded": sys.argv[1]}))
