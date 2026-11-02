#!/usr/bin/env python3
"""想起（検索・プロファイルの見出し・要約の追いつき・台帳の並び・評価の構成）の
回帰テスト。

実運用のメモリ監査で見つかった取りこぼしを固定する:
- 2文字の検索語が「最古から12件」を返していた（LIKE に並び順が無かった）
- 応答中chの除外が候補を選んだ後で効き、補充されず0件になっていた
- 「誰に聞けばいいか」が全員「**役割・担当・得意分野:**」＝見出しだけだった
- エージェントが出ないチャンネルの要約が育たなかった
- 台帳検索が新しい順だけで、一致の多い古い記録が窓から落ちていた
- 回帰評価が本番と違うツール集合を見ていた
"""

import os
import tempfile
import unittest

from core import db
from core import glossary
from core import profiles
from core import search
from core.archive_tools import launch


class _DbCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.path)

    def tearDown(self):
        os.unlink(self.path)


class SearchOrderTest(_DbCase):
    """古い発言ばかり引いて「いまどうなってる？」に古い根拠で答える問題。"""

    def setUp(self):
        super().setUp()
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text")
            db.upsert_channel(conn, id=2, name="soudan", type="text")
            db.upsert_user(conn, id=10, name="u10", display_name="ユーザー")
            # 2文字の語（trigram に乗らず LIKE へ落ちる）を30件、古い順に採番
            for i in range(30):
                ch = 2 if i % 2 else 1
                db.insert_message(
                    conn, id=100 + i, channel_id=ch, author_id=10,
                    content=f"在庫の話 その{i}",
                    created_at=f"2026-0{1 + i // 15}-01T10:00")

    def _ids(self, kws=("在庫",), **kw):
        return [r["id"] for r in search.search_messages(self.path, list(kws),
                                                        **kw)]

    def _channels(self, ids):
        with db.connect(self.path) as conn:
            return {conn.execute("SELECT channel_id FROM messages WHERE id=?",
                                 (i,)).fetchone()[0] for i in ids}

    def test_short_keyword_returns_newest_not_oldest(self):
        ids = self._ids(limit=5, per_keyword=5)
        self.assertEqual(ids, [129, 128, 127, 126, 125])
        self.assertNotIn(100, ids)

    def test_excluded_channel_is_backfilled(self):
        """除外は候補を選ぶ前に効く＝残りのchから補充される（以前は0件だった）。"""
        ids = self._ids(limit=5, per_keyword=5, exclude_channel_id=2)
        self.assertEqual(len(ids), 5)
        self.assertEqual(self._channels(ids), {1})

    def test_recent_window_only_excludes_newer_part(self):
        """recent_from_id 指定時は、同じchの古いログは候補に残る。"""
        ids = self._ids(limit=30, per_keyword=30, exclude_channel_id=2,
                        recent_from_id=120)
        self.assertIn(119, ids)         # ch2 の古い発言は残る
        self.assertNotIn(121, ids)      # ch2 の直近（会話に載る分）は外す
        self.assertIn(128, ids)         # ch1 は全部残る

    def test_long_keyword_also_honors_exclusion(self):
        ids = self._ids(("在庫の話",), limit=5, per_keyword=5,
                        exclude_channel_id=2)
        self.assertTrue(ids)
        self.assertEqual(self._channels(ids), {1})


class AttachmentNamesTest(_DbCase):
    """件数だけだと「あの資料」を名前で思い出せない。"""

    def test_names_reach_the_context(self):
        with db.connect(self.path) as conn:
            db.upsert_channel(conn, id=1, name="general", type="text")
            db.upsert_user(conn, id=10, name="u10", display_name="ユーザー")
            db.insert_message(conn, id=5, channel_id=1, author_id=10,
                              content="見積もりを置きます",
                              created_at="2026-10-01T10:00")
            db.insert_attachment(conn, id=1, message_id=5,
                                 filename="見積書_v2.pdf",
                                 content_type="application/pdf", size=10)
        rows = search.search_messages(self.path, ["見積もり"])
        self.assertEqual(rows[0]["att_names"], "見積書_v2.pdf")
        ctx = search.build_context(rows, "1")
        self.assertIn("見積書_v2.pdf", ctx)

    def test_name_display_caps(self):
        self.assertEqual(search._attachment_names(""), "")
        self.assertEqual(search._attachment_names(None), "")
        many = " / ".join(f"f{i}.png" for i in range(5))
        self.assertEqual(search._attachment_names(many),
                         "f0.png / f1.png / f2.png ほか2件")
        long = "あ" * 60
        self.assertEqual(search._attachment_names(long), "あ" * 40 + "…")

    def test_rows_without_names_are_unchanged(self):
        row = {"id": 1, "channel_id": 1, "channel": "g", "author": "a",
               "content": "x", "created_at": "2026-10-01", "imgs": 0,
               "vids": 0, "atts": 0}
        self.assertNotIn("（", search.build_context([row], "1"))


class HeadlineTest(unittest.TestCase):
    """「誰に聞けばいいか」が見出しだけになる問題。"""

    REAL = ("**役割・担当:**\n・グッズ企画、備品手配\n"
            "・イベント運営\n\n**進行中の関心事:**\n・新商品")

    def test_skips_the_heading_and_takes_the_content(self):
        self.assertEqual(profiles.headline(self.REAL), "グッズ企画、備品手配")

    def test_heading_shapes(self):
        for text in ("**役割・担当**\n開発リード", "# 役割\n開発リード",
                     "**役割・担当・得意分野:**\n開発リード"):
            self.assertEqual(profiles.headline(text), "開発リード", text)

    def test_bold_content_is_not_treated_as_a_heading(self):
        self.assertEqual(profiles.headline("**重要**: 在庫の判断役"),
                         "重要**: 在庫の判断役")

    def test_bullets_and_caps(self):
        self.assertEqual(profiles.headline("- デザイナー"), "デザイナー")
        self.assertEqual(len(profiles.headline("あ" * 200)), 60)

    def test_empty_and_heading_only(self):
        self.assertEqual(profiles.headline(""), "")
        self.assertEqual(profiles.headline(None), "")
        self.assertEqual(profiles.headline("**役割・担当:**"), "")


class StaleSummaryTest(_DbCase):
    """要約の追いつき対象の選び方（更新が応答後だけで止まっていた問題）。"""

    def setUp(self):
        super().setUp()
        with db.connect(self.path) as conn:
            for cid, name in ((1, "a"), (2, "b"), (3, "c")):
                db.upsert_channel(conn, id=cid, name=name, type="text")
            db.upsert_user(conn, id=10, name="u10", display_name="ユーザー")
            mid = 1
            for cid, n in ((1, 10), (2, 3), (3, 20)):
                for _ in range(n):
                    db.insert_message(conn, id=mid, channel_id=cid,
                                      author_id=10, content="x",
                                      created_at="2026-10-01T10:00")
                    mid += 1
            # ch3 は途中まで要約済み（残り5件＝閾値未満）
            db.upsert_thread_summary(conn, channel_id=3, summary="s",
                                     covered_until=28, updated_at="t")

    def _stale(self, **kw):
        with db.connect(self.path) as conn:
            return db.stale_summary_channels(conn, 6, **kw)

    def test_picks_the_biggest_backlog_and_skips_small_ones(self):
        got = self._stale(limit=5)
        self.assertEqual([r["channel_id"] for r in got], [1])
        self.assertEqual(got[0]["new_messages"], 10)

    def test_counts_from_covered_until(self):
        with db.connect(self.path) as conn:
            db.upsert_thread_summary(conn, channel_id=3, summary="s",
                                     covered_until=20, updated_at="t")
        self.assertEqual([r["channel_id"] for r in self._stale(limit=5)],
                         [3, 1])

    def test_empty_exclusion_does_not_drop_everything(self):
        """NOT IN (NULL) は全行を落とす（SQLの3値論理）。空リストで壊れないこと。"""
        self.assertEqual(len(self._stale(limit=5, exclude_channel_ids=())), 1)
        self.assertEqual(len(self._stale(limit=5, exclude_channel_ids=[])), 1)
        self.assertEqual(len(self._stale(limit=5, exclude_channel_ids=None)), 1)

    def test_exclusion_works(self):
        self.assertEqual(self._stale(limit=5, exclude_channel_ids=(1,)), [])

    def test_empty_content_does_not_count(self):
        """画像だけの発言は要約の材料にならない（数えると同じchを選び続ける）。"""
        with db.connect(self.path) as conn:
            conn.execute("UPDATE messages SET content='' WHERE channel_id=1")
        self.assertEqual(self._stale(limit=5), [])

    def test_deleted_messages_do_not_count(self):
        with db.connect(self.path) as conn:
            conn.execute("UPDATE messages SET deleted=1 WHERE channel_id=1")
        self.assertEqual(self._stale(limit=5), [])


class LedgerRelevanceTest(_DbCase):
    """台帳は一致語数の多い順 → 新しい順（広い語で古い決定が窓から落ちていた）。"""

    def setUp(self):
        super().setUp()
        with db.connect(self.path) as conn:
            db.add_decision(conn, agent_id="agent1", decision="新商品の価格は3000円",
                            topic="新商品の価格", source_kind="conversation",
                            source_message_id=1, channel_id=1,
                            decided_on="2026-09-01", created_at="t")
            for i in range(5):
                db.add_decision(conn, agent_id="agent1",
                                decision=f"新商品の撮影 その{i}",
                                topic="撮影", source_kind="conversation",
                                source_message_id=10 + i, channel_id=1,
                                decided_on="2026-10-01", created_at="t")
            db.add_fact(conn, agent_id="agent1", topic="送料",
                        fact="国内送料は800円", source_kind="conversation",
                        source_message_id=1, channel_id=1, stated_by="a",
                        created_at="t")
            db.add_fact(conn, agent_id="agent1", topic="在庫",
                        fact="在庫の送料は別", source_kind="conversation",
                        source_message_id=2, channel_id=1, stated_by="a",
                        created_at="t")

    def test_decisions_rank_by_matched_terms_first(self):
        with db.connect(self.path) as conn:
            rows = db.search_decisions(conn, ["新商品", "価格"], limit=2)
        self.assertEqual(rows[0]["topic"], "新商品の価格")

    def test_facts_rank_by_matched_terms_first(self):
        with db.connect(self.path) as conn:
            rows = db.search_facts(conn, ["送料", "国内"], limit=2)
        self.assertEqual([r["topic"] for r in rows], ["送料", "在庫"])

    def test_empty_keywords_return_recent(self):
        with db.connect(self.path) as conn:
            rows = db.search_decisions(conn, [], limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["decision"], "新商品の撮影 その4")


class SynonymsContextTest(unittest.TestCase):
    """事前注入を切った構成でも表記ゆれ辞書が回答の文脈に届く。"""

    def test_block_lists_pairs(self):
        out = glossary.build_synonyms_context([("いんすた", "インスタ")])
        self.assertIn("表記ゆれ", out)
        self.assertIn("インスタ（誤記: いんすた）", out)

    def test_empty_is_empty(self):
        self.assertEqual(glossary.build_synonyms_context([]), "")


class SkillsForTest(unittest.TestCase):
    """回答品質の回帰評価が本番と同じツールを見るための集合（純粋関数）。"""

    def test_mirrors_the_production_gates(self):
        agent = {"id": "agent1", "archiver": True,
                 "skills": {"reminder": True},
                 "proactive": {"minutes_channel_id": 5}}
        self.assertEqual(launch.skills_for(agent),
                         frozenset({"reminder", "action_tracking", "quota"}))

    def test_plain_agent(self):
        plain = {"id": "agent2", "skills": {"reminder": True}}
        self.assertEqual(launch.skills_for(plain), frozenset({"reminder"}))
        off = {"id": "agent2", "skills": {"reminder": False}}
        self.assertEqual(launch.skills_for(off), frozenset())

    def test_no_skills_at_all(self):
        self.assertEqual(launch.skills_for({}), frozenset())
        self.assertEqual(launch.skills_for(None), frozenset())


class GoldenEvalDefaultsTest(unittest.TestCase):
    """評価の既定は本番の tool_loop 設定（違う構成を測っていた問題）。"""

    def test_defaults_follow_the_agent_config(self):
        from core import golden_eval
        agent = {"tool_loop": {"prompt_style": "v4", "inject_search_hits": 0,
                               "inject_facts": False}}
        self.assertEqual(golden_eval.production_variant(agent),
                         {"inject_search_hits": 0, "inject_facts": False,
                          "prompt_style": "v4"})

    def test_cli_overrides_win(self):
        from core import golden_eval
        agent = {"tool_loop": {"prompt_style": "v4", "inject_search_hits": 0}}
        got = golden_eval.production_variant(
            agent, inject_search=12, no_inject_facts=True, prompt="v3")
        self.assertEqual(got, {"inject_search_hits": 12, "inject_facts": False,
                               "prompt_style": "v3"})


if __name__ == "__main__":
    unittest.main()
