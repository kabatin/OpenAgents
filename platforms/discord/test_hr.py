#!/usr/bin/env python3
"""自己増殖（agents/hr/webhooks採用解雇）のユニットテスト。"""

from types import SimpleNamespace
from unittest.mock import patch
import os
import tempfile
import unittest

from core import db
from core import hr
from platforms.discord import webhooks


class AgentsRegistryDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _add(self, conn, id="keiri", name="経理係", home=123):
        db.add_agent(conn, id=id, kind="webhook", name=name,
                     avatar_url=None, home_channel_id=home,
                     persona_file="personas/keiri.md",
                     skills_json="{}", allowed_tools_json="[]",
                     created_at="2026-07-17T00:00")

    def test_add_and_get_active(self):
        with db.connect(self.db_path) as conn:
            self._add(conn)
            agents = db.get_active_agents(conn)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["name"], "経理係")
        self.assertEqual(agents[0]["home_channel_id"], 123)
        self.assertEqual(agents[0]["kind"], "webhook")

    def test_upsert_updates(self):
        with db.connect(self.db_path) as conn:
            self._add(conn, name="旧名")
            self._add(conn, name="新名")  # 同id再登録で上書き
            agents = db.get_active_agents(conn)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["name"], "新名")

    def test_retire_excludes_from_active(self):
        with db.connect(self.db_path) as conn:
            self._add(conn)
            self.assertTrue(db.retire_agent(conn, "keiri"))
            self.assertEqual(db.get_active_agents(conn), [])
            self.assertFalse(db.retire_agent(conn, "keiri"))  # 既に退役

    def test_get_agent(self):
        with db.connect(self.db_path) as conn:
            self._add(conn)
            self.assertEqual(db.get_agent(conn, "keiri")["name"], "経理係")
            self.assertIsNone(db.get_agent(conn, "unknown"))


class ManageAgentsValidationTest(unittest.TestCase):
    """登録時のなりすまし・衝突チェック（HIGH対応）。"""

    def setUp(self):
        from platforms.discord import manage_agents
        self.ma = manage_agents

    def _args(self, **kw):
        base = dict(id="keiri", name="経理係", home_channel="500",
                    persona="x", avatar=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_rejects_id_collision_with_real_bot(self):
        with patch.object(self.ma, "_real_agents", return_value=[
                {"id": "agent1", "name": "エージェント1", "home_channel_id": "1"}]):
            err = self.ma._validate_add(self._args(id="agent1"), existing=[])
        self.assertIn("衝突", err)

    def test_rejects_home_collision_with_real_bot(self):
        with patch.object(self.ma, "_real_agents", return_value=[
                {"id": "agent1", "name": "エージェント1", "home_channel_id": "500"}]):
            err = self.ma._validate_add(self._args(home_channel="500"),
                                        existing=[])
        self.assertIn("本物Bot", err)

    def test_rejects_name_impersonation(self):
        with patch.object(self.ma, "_real_agents", return_value=[
                {"id": "agent1", "name": "エージェント1", "home_channel_id": "1"}]):
            err = self.ma._validate_add(
                self._args(name="エージェント1", home_channel="2"), existing=[])
        self.assertIsNotNone(err)

    def test_rejects_home_collision_with_existing_persona(self):
        with patch.object(self.ma, "_real_agents", return_value=[]):
            err = self.ma._validate_add(
                self._args(home_channel="500"),
                existing=[{"id": "other", "name": "採用係",
                           "home_channel_id": 500}])
        self.assertIn("Webhook人格", err)

    def test_accepts_valid(self):
        with patch.object(self.ma, "_real_agents", return_value=[
                {"id": "agent1", "name": "エージェント1", "home_channel_id": "1"}]):
            err = self.ma._validate_add(
                self._args(id="keiri", name="経理係", home_channel="500"),
                existing=[])
        self.assertIsNone(err)


class WebhookNameValidationTest(unittest.TestCase):
    def test_rejects_empty(self):
        self.assertIsNotNone(
            webhooks.validate_persona_name("", existing_names=set()))

    def test_rejects_too_long(self):
        self.assertIsNotNone(
            webhooks.validate_persona_name("あ" * 33, existing_names=set()))

    def test_rejects_impersonation(self):
        # 既存メンバー/Botと紛らわしい名前を弾く（なりすまし防止）
        self.assertIsNotNone(webhooks.validate_persona_name(
            "エージェント1", existing_names={"エージェント1", "管理者"}))

    def test_allows_distinct(self):
        self.assertIsNone(webhooks.validate_persona_name(
            "経理係", existing_names={"エージェント1"}))


class WebhookPostAvatarUrlTest(unittest.IsolatedAsyncioTestCase):
    """avatar_url にローカルパスを渡してもsendに漏らさない（400回帰）。"""

    class _FakeHook:
        def __init__(self):
            self.calls = []
            self.avatar = object()  # 既存アイコンあり扱い

        async def send(self, content, **kw):
            self.calls.append(kw)

    async def _run(self, avatar_url):
        hook = self._FakeHook()
        cache = {123: hook}
        chan = SimpleNamespace(id=123)
        await webhooks.post_as_persona(
            chan, name="AI経理", content="やあ", cache=cache,
            avatar_url=avatar_url)
        return hook.calls[0]

    async def test_local_path_not_passed_as_avatar_url(self):
        kw = await self._run("avatars/keiri.png")
        self.assertNotIn("avatar_url", kw)     # ローカルパスは渡さない
        self.assertEqual(kw["username"], "AI経理")

    async def test_http_url_passed(self):
        kw = await self._run("https://example.com/a.png")
        self.assertEqual(kw["avatar_url"], "https://example.com/a.png")


class HireParseTest(unittest.TestCase):
    def test_valid(self):
        h = hr.parse_hire("guuzu | AIグッズ | グッズ相談担当 | グッズ相談")
        self.assertEqual(h["new_id"], "guuzu")
        self.assertEqual(h["name"], "AIグッズ")
        self.assertEqual(h["role"], "グッズ相談担当")
        self.assertEqual(h["channel_name"], "グッズ相談")

    def test_strips_hash_from_channel(self):
        h = hr.parse_hire("saiyo | AI採用 | 採用担当 | #採用室")
        self.assertEqual(h["channel_name"], "採用室")

    def test_too_few_fields(self):
        with self.assertRaises(ValueError):
            hr.parse_hire("guuzu | AIグッズ | グッズ相談担当")

    def test_bad_id(self):
        with self.assertRaises(ValueError):
            hr.parse_hire("Guuzu大文字 | AIグッズ | 役割 | ch")
        with self.assertRaises(ValueError):
            hr.parse_hire("1num | AIグッズ | 役割 | ch")

    def test_name_too_long(self):
        with self.assertRaises(ValueError):
            hr.parse_hire("guuzu | " + "あ" * 81 + " | 役割 | ch")

    def test_channel_name_too_long(self):
        with self.assertRaises(ValueError):
            hr.parse_hire("guuzu | AIグッズ | 役割 | " + "c" * 101)

    def test_extract_markers(self):
        ans = ("なるほど、その役割は空いてますね。\n"
               "[HIRE: guuzu | AIグッズ | グッズ相談担当 | グッズ相談]")
        text, hires, errs = hr.extract_markers(ans)
        self.assertEqual(text, "なるほど、その役割は空いてますね。")
        self.assertEqual(len(hires), 1)
        self.assertEqual(errs, [])

    def test_extract_bad_marker_is_error_but_stripped(self):
        text, hires, errs = hr.extract_markers("はい[HIRE: 不足]")
        self.assertEqual(text, "はい")
        self.assertEqual(hires, [])
        self.assertEqual(len(errs), 1)


class HireValidateTest(unittest.TestCase):
    def _hire(self, new_id="guuzu", name="AIグッズ"):
        return {"new_id": new_id, "name": name, "role": "x",
                "channel_name": "c"}

    def test_ok(self):
        err = hr.validate_hire(
            self._hire(), real_ids={"agent1"}, real_names={"エージェント1"},
            existing_agents=[])
        self.assertIsNone(err)

    def test_id_collision_with_real(self):
        err = hr.validate_hire(
            self._hire(new_id="agent1"), real_ids={"agent1"},
            real_names={"エージェント1"}, existing_agents=[])
        self.assertIn("衝突", err)

    def test_id_collision_with_existing(self):
        err = hr.validate_hire(
            self._hire(), real_ids=set(), real_names=set(),
            existing_agents=[{"id": "guuzu", "name": "既存"}])
        self.assertIn("衝突", err)

    def test_name_collision(self):
        err = hr.validate_hire(
            self._hire(name="エージェント1"), real_ids=set(),
            real_names={"エージェント1"}, existing_agents=[])
        self.assertIn("紛らわしい", err)

    def test_cap(self):
        many = [{"id": f"a{i}", "name": f"n{i}"}
                for i in range(hr.MAX_WEBHOOK_AGENTS)]
        err = hr.validate_hire(
            self._hire(), real_ids=set(), real_names=set(),
            existing_agents=many)
        self.assertIn("上限", err)

    def test_home_channel_collision_rejected(self):
        err = hr.validate_hire(
            self._hire(), real_ids={"agent1"}, real_names={"エージェント1"},
            existing_agents=[], taken_channel_id=12345)
        self.assertIn("ホームチャンネル", err)


class HireParsePipeInRoleTest(unittest.TestCase):
    def test_role_with_pipe_preserved(self):
        # 役割説明に | が入っても、両端(id,ch)を固定して中間を役割に温存
        h = hr.parse_hire("saiyo | AI採用 | 採用｜面談｜内定まで担当 | ai採用室")
        self.assertEqual(h["new_id"], "saiyo")
        self.assertEqual(h["channel_name"], "ai採用室")
        self.assertIn("面談", h["role"])


class AiChannelNameTest(unittest.TestCase):
    def test_prepends_ai(self):
        self.assertEqual(hr.ai_channel_name("事業戦略相談"), "ai事業戦略相談")
        self.assertEqual(hr.ai_channel_name("#グッズ相談"), "aiグッズ相談")

    def test_keeps_existing_ai(self):
        self.assertEqual(hr.ai_channel_name("ai経理室"), "ai経理室")
        self.assertEqual(hr.ai_channel_name("AI議事録"), "AI議事録")  # 大小無視


class AgentChannelCreatedDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_home_channel_created_flag(self):
        with db.connect(self.db_path) as conn:
            db.add_agent(conn, id="a1", kind="webhook", name="AI一号",
                         avatar_url=None, home_channel_id=10,
                         persona_file="p", skills_json="{}",
                         allowed_tools_json="[]", created_at="t",
                         home_channel_created=True)
            db.add_agent(conn, id="a2", kind="webhook", name="AI二号",
                         avatar_url=None, home_channel_id=20,
                         persona_file="p", skills_json="{}",
                         allowed_tools_json="[]", created_at="t")
            self.assertTrue(db.get_agent(conn, "a1")["home_channel_created"])
            self.assertFalse(db.get_agent(conn, "a2")["home_channel_created"])


class FireTest(unittest.TestCase):
    def test_extract_fires(self):
        text, ids = hr.extract_fires("もう不要ですね。\n[FIRE: strategy]")
        self.assertEqual(text, "もう不要ですね。")
        self.assertEqual(ids, ["strategy"])

    def test_extract_fires_dedup(self):
        _, ids = hr.extract_fires("[FIRE: a1][FIRE: a1][FIRE: b2]")
        self.assertEqual(ids, ["a1", "b2"])

    def test_fire_marker_rejects_bad_id(self):
        _, ids = hr.extract_fires("[FIRE: 大文字ダメ]")
        self.assertEqual(ids, [])

    def test_validate_fire_ok(self):
        err, target = hr.validate_fire(
            "strategy",
            existing_agents=[{"id": "strategy", "name": "AIストラテジスト"}],
            hr_agent_id="jinji")
        self.assertIsNone(err)
        self.assertEqual(target["name"], "AIストラテジスト")

    def test_validate_fire_self_forbidden(self):
        err, _ = hr.validate_fire(
            "jinji", existing_agents=[{"id": "jinji", "name": "AI鈴木"}],
            hr_agent_id="jinji")
        self.assertIn("自分自身", err)

    def test_validate_fire_not_found(self):
        err, _ = hr.validate_fire(
            "unknown", existing_agents=[], hr_agent_id="jinji")
        self.assertIn("見つかりません", err)


class PendingFiresDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_add_claim_atomic(self):
        with db.connect(self.db_path) as conn:
            fid = db.add_pending_fire(
                conn, message_id=42, target_id="strategy",
                target_name="AIストラテジスト", proposed_by="u1",
                created_at="t")
            self.assertEqual(
                db.get_pending_fire_by_message(conn, 42)["target_id"],
                "strategy")
        with db.connect(self.db_path) as conn:
            self.assertTrue(db.claim_pending_fire(conn, fid))
        with db.connect(self.db_path) as conn:
            self.assertFalse(db.claim_pending_fire(conn, fid))  # 二重不可


class HirePersonaTemplateTest(unittest.TestCase):
    def test_render_includes_name_and_role(self):
        md = hr.render_persona("AIグッズ", "グッズ相談担当")
        self.assertIn("AIグッズ", md)
        self.assertIn("グッズ相談担当", md)
        self.assertIn("正直", md)   # 誠実な失敗の指針が入る


class PendingHiresDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_add_get_and_status(self):
        with db.connect(self.db_path) as conn:
            hid = db.add_pending_hire(
                conn, message_id=555, new_id="guuzu", name="AIグッズ",
                role="役割", channel_name="グッズ相談", channel_id=None,
                proposed_by="u1", created_at="t")
            self.assertTrue(hid)
            got = db.get_pending_hire_by_message(conn, 555)
            self.assertEqual(got["new_id"], "guuzu")
            self.assertIsNone(got["channel_id"])
            db.set_hire_status(conn, hid, "done")
            self.assertIsNone(db.get_pending_hire_by_message(conn, 555))

    def test_claim_is_atomic_single_winner(self):
        # 二重承認→二重spawnの防止: claimは1回だけ成功する
        with db.connect(self.db_path) as conn:
            hid = db.add_pending_hire(
                conn, message_id=777, new_id="guuzu", name="AIグッズ",
                role="役割", channel_name="c", channel_id=None,
                proposed_by="u1", created_at="t")
        with db.connect(self.db_path) as conn:
            self.assertTrue(db.claim_pending_hire(conn, hid))   # 1回目=勝ち
        with db.connect(self.db_path) as conn:
            self.assertFalse(db.claim_pending_hire(conn, hid))  # 2回目=負け
            # 確保後は get（pending限定）に載らない
            self.assertIsNone(db.get_pending_hire_by_message(conn, 777))
