#!/usr/bin/env python3
"""採用/解雇の実行フロー統合テスト（fake discordで最重要フローを検証）。

hire/fire は「AIが提案→人間👍→実行」の一番危険な経路。純粋ロジック
（hr.py）とは別に、WebhookPersonaMixin の承認ゲートと実行副作用
（チャンネル作成/削除・台帳更新・アーカイブ保全）を、Discordに繋がずに
fakeオブジェクトで検証する。"""

import os
import tempfile
import unittest
from unittest.mock import patch

from platforms.discord import agent_runtime
from core import db
from platforms.discord import webhooks
from platforms.discord import webhook_personas
from platforms.discord.webhook_personas import WebhookPersonaMixin


# ---- fake discord オブジェクト -------------------------------------------

class _FakeChannel:
    def __init__(self, id, name="ai-x"):
        self.id = id
        self.name = name
        self.deleted = False
        self.delete_reason = None
        self.sent = []

    async def delete(self, reason=None):
        self.deleted = True
        self.delete_reason = reason

    async def send(self, *a, **k):
        self.sent.append((a, k))


class _FakeGuild:
    def __init__(self, channels=None):
        self.text_channels = list(channels or [])
        self._by_id = {c.id: c for c in self.text_channels}
        self.created = []

    def get_channel(self, cid):
        return self._by_id.get(cid)

    async def create_text_channel(self, name, category=None):
        ch = _FakeChannel(id=9000 + len(self.created), name=name)
        self.created.append(ch)
        self._by_id[ch.id] = ch
        self.text_channels.append(ch)
        return ch


class _FakeUser:
    def __init__(self, id):
        self.id = id


class _FakePayload:
    def __init__(self, *, guild_id, user_id, emoji, message_id, channel_id=1):
        self.guild_id = guild_id
        self.user_id = user_id
        self.emoji = emoji
        self.message_id = message_id
        self.channel_id = channel_id


class _Harness(WebhookPersonaMixin):
    """テスト用の最小 AgentClient 相当（Mixinが要る協調メソッドだけ用意）。"""

    def __init__(self, guild, proposal_ch):
        self._guild = guild
        self._proposal = proposal_ch
        self._bg_tasks = set()
        self.user = _FakeUser(999)
        self.agent = {"id": "agent1", "name": "エージェント1"}
        self.executed = []           # 承認ゲート検証用（実行到達を記録）

    def get_channel(self, cid):
        return self._proposal

    def get_guild(self, gid):
        return self._guild

    # 本体（bot.py）側のメソッドはここでは無害なスタブにする
    def _apply_rule_markers(self, *a, **k):
        return ""

    async def _update_thread_summary(self, *a, **k):
        return None

    # Codex呼び出しを避ける（アイコン生成はスキップ扱い）
    def _generate_agent_avatar(self, new_id, name, role):
        return None


async def _noop_post(channel, **kw):
    """webhooks.post_as_persona の差し替え（投稿内容を記録）。"""
    _noop_post.calls.append((getattr(channel, "id", None), kw))
    return None


_noop_post.calls = []


class _HrFlowBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "archive.db")
        os.makedirs(os.path.join(self.tmp, "personas"))
        db.init_db(self.db_path)
        # DB_PATH は mixin側と _load_webhook_agents(agent_runtime側)の両方が見る
        self._save = {
            "wp_db": webhook_personas.DB_PATH,
            "ar_db": agent_runtime.DB_PATH,
            "wp_resolve": webhook_personas._resolve,
            "guild": webhook_personas.GUILD_ID,
            "admins": webhook_personas.ADMIN_IDS,
            "approve": webhook_personas.APPROVE_REACTIONS,
            "agents": webhook_personas.AGENTS,
            "post": webhooks.post_as_persona,
        }
        webhook_personas.DB_PATH = self.db_path
        agent_runtime.DB_PATH = self.db_path
        webhook_personas._resolve = lambda p: os.path.join(self.tmp, p)
        webhook_personas.GUILD_ID = 42
        webhook_personas.ADMIN_IDS = {"admin1"}
        webhook_personas.APPROVE_REACTIONS = {"👍"}
        webhook_personas.AGENTS = []   # 本物Botのホーム衝突は各テストで指定
        webhooks.post_as_persona = _noop_post
        _noop_post.calls = []
        # 共有状態は退避（テストが台帳をreloadで書き換えるため）
        self._wa = dict(agent_runtime.WEBHOOK_AGENTS)
        self._wh = dict(agent_runtime.WEBHOOK_HOME_CHANNELS)

    def tearDown(self):
        webhook_personas.DB_PATH = self._save["wp_db"]
        agent_runtime.DB_PATH = self._save["ar_db"]
        webhook_personas._resolve = self._save["wp_resolve"]
        webhook_personas.GUILD_ID = self._save["guild"]
        webhook_personas.ADMIN_IDS = self._save["admins"]
        webhook_personas.APPROVE_REACTIONS = self._save["approve"]
        webhook_personas.AGENTS = self._save["agents"]
        webhooks.post_as_persona = self._save["post"]
        agent_runtime.WEBHOOK_AGENTS.clear()
        agent_runtime.WEBHOOK_AGENTS.update(self._wa)
        agent_runtime.WEBHOOK_HOME_CHANNELS.clear()
        agent_runtime.WEBHOOK_HOME_CHANNELS.update(self._wh)

    def _seed_hire(self, *, new_id="guuzu", name="AIグッズ", role="グッズ相談",
                   channel_name="aiグッズ相談", channel_id=None, msg_id=555):
        with db.connect(self.db_path) as conn:
            db.add_pending_hire(
                conn, message_id=msg_id, new_id=new_id, name=name, role=role,
                channel_name=channel_name, channel_id=channel_id,
                proposed_by="u1", created_at="2026-07-17 00:00")
            return db.get_pending_hire_by_message(conn, msg_id)

    def _seed_agent(self, *, id="guuzu", name="AIグッズ", home_channel_id=1234,
                    created=True):
        with db.connect(self.db_path) as conn:
            db.add_agent(
                conn, id=id, kind="webhook", name=name, avatar_url=None,
                home_channel_id=home_channel_id, persona_file="personas/x.md",
                skills_json="{}", allowed_tools_json="[]",
                created_at="2026-07-17 00:00", home_channel_created=created)


# ---- 承認ゲート（誰が/何で承認できるか） ---------------------------------

class ApproveHireGateTest(_HrFlowBase):
    async def _run(self, payload):
        h = _Harness(_FakeGuild(), _FakeChannel(1))
        with patch.object(_Harness, "_execute_hire",
                          new=_record_execute("hire")):
            await h._maybe_approve_hire(payload)
        return h

    async def test_valid_admin_thumbsup_executes(self):
        self._seed_hire()
        h = await self._run(_FakePayload(
            guild_id=42, user_id="admin1", emoji="👍", message_id=555))
        self.assertEqual(len(h.executed), 1)

    async def test_wrong_guild_ignored(self):
        self._seed_hire()
        h = await self._run(_FakePayload(
            guild_id=999, user_id="admin1", emoji="👍", message_id=555))
        self.assertEqual(h.executed, [])

    async def test_non_admin_ignored(self):
        self._seed_hire()
        h = await self._run(_FakePayload(
            guild_id=42, user_id="hacker", emoji="👍", message_id=555))
        self.assertEqual(h.executed, [])

    async def test_non_approve_emoji_ignored(self):
        self._seed_hire()
        h = await self._run(_FakePayload(
            guild_id=42, user_id="admin1", emoji="❤️", message_id=555))
        self.assertEqual(h.executed, [])

    async def test_no_pending_ignored(self):
        h = await self._run(_FakePayload(
            guild_id=42, user_id="admin1", emoji="👍", message_id=555))
        self.assertEqual(h.executed, [])

    async def test_double_approve_executes_once(self):
        self._seed_hire()
        payload = _FakePayload(
            guild_id=42, user_id="admin1", emoji="👍", message_id=555)
        h = _Harness(_FakeGuild(), _FakeChannel(1))
        with patch.object(_Harness, "_execute_hire",
                          new=_record_execute("hire")):
            await h._maybe_approve_hire(payload)
            await h._maybe_approve_hire(payload)   # 2度目はclaim負けで無視
        self.assertEqual(len(h.executed), 1)


class ApproveFireGateTest(_HrFlowBase):
    def _seed_fire(self, msg_id=777, target_id="guuzu"):
        self._seed_agent(id=target_id)
        with db.connect(self.db_path) as conn:
            db.add_pending_fire(
                conn, message_id=msg_id, target_id=target_id,
                target_name="AIグッズ", proposed_by="u1",
                created_at="2026-07-17 00:00")

    async def _run(self, payload):
        h = _Harness(_FakeGuild(), _FakeChannel(1))
        with patch.object(_Harness, "_execute_fire",
                          new=_record_execute("fire")):
            await h._maybe_approve_fire(payload)
        return h

    async def test_valid_admin_executes(self):
        self._seed_fire()
        h = await self._run(_FakePayload(
            guild_id=42, user_id="admin1", emoji="👍", message_id=777))
        self.assertEqual(len(h.executed), 1)

    async def test_non_admin_ignored(self):
        self._seed_fire()
        h = await self._run(_FakePayload(
            guild_id=42, user_id="hacker", emoji="👍", message_id=777))
        self.assertEqual(h.executed, [])


def _record_execute(kind):
    async def _fn(self, payload, obj):
        self.executed.append((kind, obj))
    return _fn


# ---- 解雇の実行（退役・部屋削除・アーカイブ保全） -------------------------

class ExecuteFireTest(_HrFlowBase):
    async def test_retire_and_delete_created_channel_keeps_archive(self):
        home = 1234
        self._seed_agent(id="guuzu", home_channel_id=home, created=True)
        # アーカイブに1件（この部屋のログ）→ 解雇後も残ることを確認
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=home, name="aiグッズ相談",
                              type="text", parent_id=None)
            db.insert_message(conn, id=1, channel_id=home, author_id=7,
                              content="在庫どう?", created_at="2026-07-17 00:00")
            fire = _post_and_fetch_fire(conn, home_target="guuzu")
        ch = _FakeChannel(home, name="aiグッズ相談")
        guild = _FakeGuild([ch])
        h = _Harness(guild, _FakeChannel(1))
        payload = _FakePayload(guild_id=42, user_id="admin1", emoji="👍",
                               message_id=888, channel_id=1)
        await h._execute_fire(payload, fire)
        # 退役済み・部屋削除・でもアーカイブは残る
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.get_agent(conn, "guuzu")["status"], "retired")
            self.assertNotIn("guuzu",
                             {a["id"] for a in db.get_active_agents(conn)})
            self.assertEqual(db.last_message_id(conn, home), 1)  # ログ保全
        self.assertTrue(ch.deleted)

    async def test_keep_channel_when_not_created_by_hire(self):
        home = 4321
        self._seed_agent(id="mochi", home_channel_id=home, created=False)
        with db.connect(self.db_path) as conn:
            fire = _post_and_fetch_fire(conn, home_target="mochi", msg_id=889)
        ch = _FakeChannel(home)
        guild = _FakeGuild([ch])
        h = _Harness(guild, _FakeChannel(1))
        payload = _FakePayload(guild_id=42, user_id="admin1", emoji="👍",
                               message_id=889)
        await h._execute_fire(payload, fire)
        self.assertFalse(ch.deleted)          # 自前で作った部屋でないので残す


# ---- 採用の実行（再検証→作成→登録） -------------------------------------

class ExecuteHireTest(_HrFlowBase):
    async def test_happy_path_creates_channel_and_registers(self):
        hire = self._seed_hire(new_id="guuzu", channel_name="aiグッズ相談")
        guild = _FakeGuild()
        h = _Harness(guild, _FakeChannel(1))
        payload = _FakePayload(guild_id=42, user_id="admin1", emoji="👍",
                               message_id=555)
        await h._execute_hire(payload, hire)
        # 新チャンネルが「ai」接頭辞つきで作られ、台帳に登録される
        self.assertEqual(len(guild.created), 1)
        self.assertTrue(guild.created[0].name.startswith("ai"))
        with db.connect(self.db_path) as conn:
            agent = db.get_agent(conn, "guuzu")
        self.assertIsNotNone(agent)
        self.assertEqual(agent["status"], "active")
        self.assertTrue(agent["home_channel_created"])

    async def test_revalidation_rejects_home_collision(self):
        # 配属先が本物Botのホームと重複 → 再検証で拒否・台帳登録しない
        taken = _FakeChannel(1500, name="ai戦略室")
        webhook_personas.AGENTS = [{"id": "agent1", "name": "エージェント1",
                                    "home_channel_id": 1500}]
        hire = self._seed_hire(new_id="kabu", channel_name="ai戦略室",
                               channel_id=1500, msg_id=560)
        guild = _FakeGuild([taken])
        h = _Harness(guild, _FakeChannel(1))
        payload = _FakePayload(guild_id=42, user_id="admin1", emoji="👍",
                               message_id=560)
        await h._execute_hire(payload, hire)
        with db.connect(self.db_path) as conn:
            self.assertIsNone(db.get_agent(conn, "kabu"))   # 採用されない
            hire2 = conn.execute(
                "SELECT status FROM pending_hires WHERE new_id='kabu'"
            ).fetchone()
        self.assertEqual(hire2[0], "rejected")
        self.assertEqual(guild.created, [])                 # 部屋も作らない


def _post_and_fetch_fire(conn, *, home_target, msg_id=888):
    db.add_pending_fire(conn, message_id=msg_id, target_id=home_target,
                        target_name="退役対象", proposed_by="u1",
                        created_at="2026-07-17 00:00")
    return db.get_pending_fire_by_message(conn, msg_id)


if __name__ == "__main__":
    unittest.main()
