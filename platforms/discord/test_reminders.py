#!/usr/bin/env python3
"""リマインダー（reminders・宛先解決）のユニットテスト。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
import os
import tempfile
import unittest

from platforms.discord import agent_runtime
from platforms.discord import bot
from platforms.discord import marker_actions
from core import reminders


NOW = datetime(2026, 7, 3, 23, 0)  # 2026-07-03(金) 23:00 JST 固定


class ExtractRemindMarkersTest(unittest.TestCase):
    def test_no_marker(self):
        text, adds, cancels, errors = reminders.extract_markers("普通の回答")
        self.assertEqual(text, "普通の回答")
        self.assertEqual((adds, cancels, errors), ([], [], []))

    def test_once(self):
        text, adds, _, errors = reminders.extract_markers(
            "了解っス！\n[REMIND: 2026-07-04T09:00 | once | ゴミ出し]")
        self.assertEqual(text, "了解っス！")
        self.assertEqual(errors, [])
        self.assertEqual(adds[0]["due"], datetime(2026, 7, 4, 9, 0))
        self.assertEqual(adds[0]["repeat"], "once")
        self.assertIsNone(adds[0]["to"])
        self.assertEqual(adds[0]["content"], "ゴミ出し")

    def test_repeat_omitted_falls_back_to_once(self):
        _, adds, _, errors = reminders.extract_markers(
            "[REMIND: 2026-07-04 09:00 | ゴミ出し]")
        self.assertEqual(errors, [])
        self.assertEqual(adds[0]["repeat"], "once")
        self.assertEqual(adds[0]["content"], "ゴミ出し")

    def test_weekly_with_to(self):
        _, adds, _, _ = reminders.extract_markers(
            "[REMIND: 2026-07-06T10:00 | weekly | to=@チーム | 定例準備]")
        self.assertEqual(adds[0]["repeat"], "weekly")
        self.assertEqual(adds[0]["to"], "チーム")
        self.assertEqual(adds[0]["content"], "定例準備")

    def test_content_with_pipe_preserved(self):
        _, adds, _, _ = reminders.extract_markers(
            "[REMIND: 2026-07-04T09:00 | once | A案|B案 どちらか決める]")
        self.assertEqual(adds[0]["content"], "A案|B案 どちらか決める")

    def test_content_starting_like_to_not_confused(self):
        _, adds, _, _ = reminders.extract_markers(
            "[REMIND: 2026-07-04T09:00 | once | tokyoの件を確認]")
        self.assertIsNone(adds[0]["to"])
        self.assertEqual(adds[0]["content"], "tokyoの件を確認")

    def test_multiple_markers(self):
        _, adds, _, _ = reminders.extract_markers(
            "[REMIND: 2026-07-04T09:00 | daily | 朝会]\n"
            "[REMIND: 2026-07-04T18:00 | daily | 日報]")
        self.assertEqual(len(adds), 2)

    def test_invalid_datetime_becomes_error_and_marker_removed(self):
        text, adds, _, errors = reminders.extract_markers(
            "登録したっス！\n[REMIND: あした9じ | once | ゴミ出し]")
        self.assertEqual(text, "登録したっス！")  # 生マーカーは残らない
        self.assertEqual(adds, [])
        self.assertEqual(len(errors), 1)

    def test_missing_content_is_error(self):
        _, adds, _, errors = reminders.extract_markers(
            "[REMIND: 2026-07-04T09:00 | weekly]")
        self.assertEqual(adds, [])
        self.assertEqual(len(errors), 1)

    def test_cancel_multiple(self):
        text, _, cancels, _ = reminders.extract_markers(
            "全部止めるっスね\n[REMIND_CANCEL: 3]\n[REMIND_CANCEL: 7]")
        self.assertEqual(text, "全部止めるっスね")
        self.assertEqual(cancels, [3, 7])

    def test_marker_only_empty_text(self):
        text, adds, _, _ = reminders.extract_markers(
            "[REMIND: 2026-07-04T09:00 | once | x]")
        self.assertEqual(text, "")
        self.assertEqual(len(adds), 1)


class NextOccurrenceTest(unittest.TestCase):
    def occ(self, y, mo, d, repeat, anchor):
        return reminders.next_occurrence(
            datetime(y, mo, d, 9, 0), repeat, anchor)

    def test_daily(self):
        self.assertEqual(self.occ(2026, 7, 3, "daily", 3),
                         datetime(2026, 7, 4, 9, 0))

    def test_weekly(self):
        self.assertEqual(self.occ(2026, 7, 3, "weekly", 3),
                         datetime(2026, 7, 10, 9, 0))

    def test_monthly_normal(self):
        self.assertEqual(self.occ(2026, 7, 15, "monthly", 15),
                         datetime(2026, 8, 15, 9, 0))

    def test_monthly_jan31_to_feb28(self):
        self.assertEqual(self.occ(2026, 1, 31, "monthly", 31),
                         datetime(2026, 2, 28, 9, 0))  # 2026は平年

    def test_monthly_jan31_leap_year(self):
        self.assertEqual(self.occ(2028, 1, 31, "monthly", 31),
                         datetime(2028, 2, 29, 9, 0))  # 2028は閏年

    def test_monthly_anchor_recovers_after_feb(self):
        self.assertEqual(self.occ(2026, 2, 28, "monthly", 31),
                         datetime(2026, 3, 31, 9, 0))

    def test_monthly_year_rollover(self):
        self.assertEqual(self.occ(2026, 12, 31, "monthly", 31),
                         datetime(2027, 1, 31, 9, 0))

    def test_monthly_end_from_short_month(self):
        # 9/30登録でも月末追従が崩れない（monthlyだと10/30になるケース）
        self.assertEqual(self.occ(2026, 9, 30, "monthly_end", 30),
                         datetime(2026, 10, 31, 9, 0))

    def test_monthly_end_into_february(self):
        self.assertEqual(self.occ(2026, 1, 31, "monthly_end", 31),
                         datetime(2026, 2, 28, 9, 0))

    def test_advance_past_collapses_missed_runs(self):
        # 3日前due のdailyは、now直後の本来時刻1ステップ分だけになる
        due = reminders.advance_past(
            datetime(2026, 6, 30, 9, 0), "daily", 30, NOW)
        self.assertEqual(due, datetime(2026, 7, 4, 9, 0))


class RemindersStateTest(unittest.TestCase):
    def setUp(self):
        self._orig = reminders.STATE_FILE
        self._dir = tempfile.TemporaryDirectory()
        reminders.STATE_FILE = os.path.join(self._dir.name, "reminders.json")

    def tearDown(self):
        reminders.STATE_FILE = self._orig
        self._dir.cleanup()

    def add(self, due, repeat="once", user_id="100", **kw):
        return reminders.add_reminder(
            channel_id="1", user_id=user_id, user_name="tester",
            content=kw.pop("content", "テスト"), due=due, repeat=repeat,
            now=NOW, **kw)

    def test_add_and_list_roundtrip_with_sequential_ids(self):
        e1, err1 = self.add(datetime(2026, 7, 4, 9, 0))
        e2, err2 = self.add(datetime(2026, 7, 5, 9, 0))
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual((e1["id"], e2["id"]), (1, 2))
        self.assertEqual(len(reminders.list_active("100")), 2)
        self.assertEqual(reminders.list_active("999"), [])

    def test_past_once_within_grace_is_accepted(self):
        _, err = self.add(datetime(2026, 7, 3, 22, 55))  # 5分前
        self.assertIsNone(err)
        self.assertEqual(len(reminders.due_reminders(NOW)), 1)

    def test_past_once_beyond_grace_is_rejected(self):
        e, err = self.add(datetime(2026, 7, 3, 9, 0))  # 14時間前
        self.assertIsNone(e)
        self.assertIn("過去", err)

    def test_past_repeat_advances_to_future(self):
        e, err = self.add(datetime(2026, 7, 3, 9, 0), repeat="daily")
        self.assertIsNone(err)
        self.assertEqual(e["due"], "2026-07-04T09:00")

    def test_cancel_owner_only(self):
        e, _ = self.add(datetime(2026, 7, 4, 9, 0))
        self.assertEqual(reminders.cancel_reminder(e["id"], "999"),
                         (None, "not_owner"))  # 他人
        entry, reason = reminders.cancel_reminder(e["id"], "100")
        self.assertIsNotNone(entry)
        self.assertIsNone(reason)
        self.assertEqual(reminders.cancel_reminder(e["id"], "100"),
                         (None, "ended"))  # 二重
        self.assertEqual(reminders.list_active(), [])

    def test_due_boundary(self):
        self.add(NOW)                                # due == now → 含む
        self.add(datetime(2026, 7, 4, 9, 0))         # 未来 → 含まない
        self.assertEqual(len(reminders.due_reminders(NOW)), 1)

    def test_mark_fired_once_becomes_done(self):
        e, _ = self.add(NOW)
        reminders.mark_fired(e["id"], now=NOW)
        self.assertEqual(reminders.list_active(), [])
        self.assertEqual(reminders.due_reminders(NOW), [])

    def test_mark_fired_repeat_advances_and_resets_fails(self):
        e, _ = self.add(datetime(2026, 7, 4, 9, 0), repeat="weekly")
        reminders.mark_failed(e["id"])
        r = reminders.mark_fired(e["id"], now=datetime(2026, 7, 4, 9, 0))
        self.assertEqual(r["due"], "2026-07-11T09:00")
        self.assertEqual(r["fail_count"], 0)
        self.assertEqual(len(reminders.list_active()), 1)

    def test_mark_failed_five_times_becomes_error(self):
        e, _ = self.add(NOW)
        for _ in range(reminders.MAX_FAIL):
            reminders.mark_failed(e["id"])
        self.assertEqual(reminders.due_reminders(NOW), [])  # 再試行停止

    def test_inactive_pruned_to_keep_limit(self):
        for i in range(reminders.KEEP_INACTIVE + 10):
            e, _ = self.add(NOW, content=f"件{i}")
            reminders.mark_fired(e["id"], now=NOW)
        state = reminders._load_state()
        self.assertEqual(len(state["reminders"]), reminders.KEEP_INACTIVE)
        # 新しい順に残る（古い「件0」は落ち、最後の件は残る）
        contents = [r["content"] for r in state["reminders"]]
        self.assertNotIn("件0", contents)
        self.assertIn(f"件{reminders.KEEP_INACTIVE + 9}", contents)

    def test_corrupt_json_recovers_empty_with_backup(self):
        with open(reminders.STATE_FILE, "w") as f:
            f.write("{broken json")
        self.assertEqual(reminders.list_active(), [])
        self.assertTrue(os.path.exists(reminders.STATE_FILE + ".bak"))

    def test_due_reminders_scoped_by_agent(self):
        self.add(NOW)  # agent_id デフォルト "agent1"
        self.assertEqual(len(reminders.due_reminders(NOW, agent_id="agent1")), 1)
        self.assertEqual(reminders.due_reminders(NOW, agent_id="agent2"), [])

    def test_active_per_user_limit(self):
        for i in range(reminders.MAX_ACTIVE_PER_USER):
            _, err = self.add(datetime(2026, 8, 1, 9, 0), content=f"件{i}")
            self.assertIsNone(err)
        e, err = self.add(datetime(2026, 8, 1, 9, 0), content="超過分")
        self.assertIsNone(e)
        self.assertIn("上限", err)
        # 他ユーザーは影響を受けない
        _, err = self.add(datetime(2026, 8, 1, 9, 0), user_id="200")
        self.assertIsNone(err)


def _fake_guild():
    role = lambda rid, name: SimpleNamespace(  # noqa: E731
        name=name, mention=f"<@&{rid}>")
    member = lambda mid, disp, name: SimpleNamespace(  # noqa: E731
        display_name=disp, name=name, mention=f"<@{mid}>")
    return SimpleNamespace(
        roles=[role(10, "チーム"), role(11, "重複"), role(12, "重複")],
        members=[member(100, "管理者", "_sato_"),
                 member(101, "同名", "a"), member(102, "同名", "b")])


class ResolveMentionTest(unittest.TestCase):
    def test_everyone_variants(self):
        for word in ("everyone", "全員", "全体", "@everyone"):
            self.assertEqual(bot._resolve_mention(_fake_guild(), word),
                             ("@everyone", "@everyone"), word)

    def test_unique_role(self):
        self.assertEqual(bot._resolve_mention(_fake_guild(), "@チーム"),
                         ("<@&10>", "@チーム"))

    def test_ambiguous_role_not_resolved(self):
        self.assertEqual(bot._resolve_mention(_fake_guild(), "重複"),
                         (None, None))

    def test_unique_member(self):
        self.assertEqual(bot._resolve_mention(_fake_guild(), "管理者"),
                         ("<@100>", "@管理者"))

    def test_ambiguous_member_not_resolved(self):
        self.assertEqual(bot._resolve_mention(_fake_guild(), "同名"),
                         (None, None))

    def test_unknown_name(self):
        self.assertEqual(bot._resolve_mention(_fake_guild(), "存在しない"),
                         (None, None))


class BroadcastGateTest(unittest.TestCase):
    def test_is_broadcast(self):
        self.assertTrue(bot._is_broadcast("@everyone"))
        self.assertTrue(bot._is_broadcast("<@&10>"))
        self.assertFalse(bot._is_broadcast("<@100>"))

    def test_can_broadcast_follows_discord_permission(self):
        allowed = SimpleNamespace(
            guild_permissions=SimpleNamespace(mention_everyone=True))
        denied = SimpleNamespace(
            guild_permissions=SimpleNamespace(mention_everyone=False))
        self.assertTrue(bot._can_broadcast(allowed))
        self.assertFalse(bot._can_broadcast(denied))
        self.assertFalse(bot._can_broadcast(SimpleNamespace()))  # 権限属性なし


class BuildSkillNoteTest(unittest.TestCase):
    def test_contains_now_with_weekday(self):
        note = reminders.build_skill_note(NOW, [])
        self.assertIn("2026-07-03(金) 23:00 JST", note)
        self.assertIn("（なし）", note)

    def test_lists_entries_and_truncates_to_ten(self):
        entries = [
            {"id": i, "due": "2026-07-06T10:00", "repeat": "weekly",
             "content": f"件{i}", "mention_label": None}
            for i in range(1, 13)
        ]
        note = reminders.build_skill_note(NOW, entries)
        self.assertIn("id=1: 07/06(月) 10:00 毎週 件1", note)
        self.assertIn("id=10:", note)
        self.assertNotIn("id=11:", note)

    def test_entry_line_shows_mention_label_and_truncates_content(self):
        line = reminders.format_entry_line(
            {"id": 3, "due": "2026-07-31T09:00", "repeat": "monthly_end",
             "content": "あ" * 50, "mention_label": "@チーム"})
        self.assertIn("毎月末", line)
        self.assertIn("宛先:@チーム", line)
        self.assertIn("あ" * 40 + "…", line)
        self.assertNotIn("あ" * 41, line)


# ---------------------------------------------------------------- 起票#4: チャンネル宛


def _perms(view=True, send=True):
    return SimpleNamespace(view_channel=view, send_messages=send)


def _fake_channel(cid, name, perms=None):
    p = perms or _perms()
    return SimpleNamespace(id=cid, name=name,
                           permissions_for=lambda member: p)


def _guild_with_channels(channels):
    by_id = {c.id: c for c in channels}
    return SimpleNamespace(text_channels=channels,
                           get_channel=lambda cid: by_id.get(cid),
                           roles=[], members=[])


class SplitChannelTokensTest(unittest.TestCase):
    def test_channel_and_person_mixed(self):
        chans, people = agent_runtime.split_channel_tokens("#金曜定例,山田")
        self.assertEqual(chans, ["#金曜定例"])
        self.assertEqual(people, "山田")

    def test_channel_mention_form(self):
        chans, people = agent_runtime.split_channel_tokens("<#555>")
        self.assertEqual(chans, ["<#555>"])
        self.assertEqual(people, "")

    def test_people_only_passes_through(self):
        chans, people = agent_runtime.split_channel_tokens("山田,田中")
        self.assertEqual(chans, [])
        self.assertEqual(people, "山田、田中")

    def test_empty(self):
        self.assertEqual(agent_runtime.split_channel_tokens(""), ([], ""))


class ResolveChannelTest(unittest.TestCase):
    def setUp(self):
        self.ch = _fake_channel(555, "金曜定例")
        self.guild = _guild_with_channels(
            [self.ch, _fake_channel(556, "重複ch"), _fake_channel(557, "重複ch")])

    def test_by_name(self):
        self.assertIs(
            agent_runtime.resolve_channel(self.guild, "#金曜定例"), self.ch)

    def test_by_mention_id(self):
        self.assertIs(
            agent_runtime.resolve_channel(self.guild, "<#555>"), self.ch)

    def test_unknown_name(self):
        self.assertIsNone(
            agent_runtime.resolve_channel(self.guild, "#存在しない"))

    def test_duplicate_name_not_resolved(self):
        self.assertIsNone(
            agent_runtime.resolve_channel(self.guild, "#重複ch"))

    def test_no_guild(self):
        self.assertIsNone(agent_runtime.resolve_channel(None, "#金曜定例"))


class CanTargetChannelTest(unittest.TestCase):
    def test_view_and_send_ok(self):
        ch = _fake_channel(555, "金曜定例", _perms(True, True))
        self.assertTrue(agent_runtime.can_target_channel(ch, object()))

    def test_no_send_denied(self):
        ch = _fake_channel(555, "金曜定例", _perms(True, False))
        self.assertFalse(agent_runtime.can_target_channel(ch, object()))

    def test_no_view_denied(self):
        ch = _fake_channel(555, "金曜定例", _perms(False, True))
        self.assertFalse(agent_runtime.can_target_channel(ch, object()))

    def test_permissions_error_denied(self):
        def boom(member):
            raise RuntimeError("no perms api")
        ch = SimpleNamespace(id=555, name="x", permissions_for=boom)
        self.assertFalse(agent_runtime.can_target_channel(ch, object()))


class ChannelTargetStateTest(unittest.TestCase):
    def setUp(self):
        self._orig = reminders.STATE_FILE
        self._dir = tempfile.TemporaryDirectory()
        reminders.STATE_FILE = os.path.join(self._dir.name, "reminders.json")

    def tearDown(self):
        reminders.STATE_FILE = self._orig
        self._dir.cleanup()

    def test_add_reminder_stores_channel_label(self):
        entry, err = reminders.add_reminder(
            channel_id="555", user_id="100", user_name="tester",
            content="YT定例", due=datetime(2026, 7, 10, 10, 0),
            repeat="weekly", channel_label="#金曜定例", now=NOW)
        self.assertIsNone(err)
        self.assertEqual(entry["channel_id"], "555")
        self.assertEqual(entry["channel_label"], "#金曜定例")
        line = reminders.format_entry_line(entry)
        self.assertIn("→#金曜定例", line)

    def test_entry_line_without_channel_label_unchanged(self):
        line = reminders.format_entry_line(
            {"id": 3, "due": "2026-07-31T09:00", "repeat": "weekly",
             "content": "定例", "mention_label": None})
        self.assertNotIn("→", line)

    def test_skill_note_documents_channel_syntax(self):
        note = reminders.build_skill_note(NOW, [])
        self.assertIn("to=#", note)


class ApplyChannelMarkerTest(unittest.TestCase):
    """_apply_reminder_markers のチャンネル宛統合（DB不要のch-onlyケース）。"""

    def setUp(self):
        self._orig = reminders.STATE_FILE
        self._dir = tempfile.TemporaryDirectory()
        reminders.STATE_FILE = os.path.join(self._dir.name, "reminders.json")
        self.fake_self = SimpleNamespace(agent={"id": "agent1"})

    def tearDown(self):
        reminders.STATE_FILE = self._orig
        self._dir.cleanup()

    def _msg(self, guild):
        return SimpleNamespace(
            guild=guild, channel=SimpleNamespace(id=111), id=999,
            author=SimpleNamespace(id=100, display_name="管理者"))

    def _apply(self, guild, answer):
        return marker_actions.MarkerActionsMixin._apply_reminder_markers(
            self.fake_self, self._msg(guild), answer)

    def test_channel_target_registers_to_that_channel(self):
        guild = _guild_with_channels([_fake_channel(555, "金曜定例")])
        out = self._apply(
            guild, "了解 [REMIND: 2030-07-12T10:00 | weekly | to=#金曜定例 | YT定例]")
        self.assertIn("→#金曜定例", out)
        active = reminders.list_active("100")
        self.assertEqual(active[0]["channel_id"], "555")
        self.assertEqual(active[0]["channel_label"], "#金曜定例")

    def test_unknown_channel_falls_back_to_current(self):
        guild = _guild_with_channels([_fake_channel(555, "金曜定例")])
        out = self._apply(
            guild, "[REMIND: 2030-07-12T10:00 | weekly | to=#無いch | YT定例]")
        self.assertIn("見つからない", out)
        self.assertEqual(reminders.list_active("100")[0]["channel_id"], "111")

    def test_no_permission_falls_back_to_current(self):
        locked = _fake_channel(555, "金曜定例", _perms(True, False))
        out = self._apply(
            _guild_with_channels([locked]),
            "[REMIND: 2030-07-12T10:00 | weekly | to=#金曜定例 | YT定例]")
        self.assertIn("書き込めない", out)
        self.assertEqual(reminders.list_active("100")[0]["channel_id"], "111")


class AdminCancelTest(unittest.TestCase):
    """管理者のフルアクセス＋正直なエラー理由（2026-08-07）。"""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.unlink(self._tmp.name)
        self._orig = reminders.STATE_FILE
        reminders.STATE_FILE = self._tmp.name

    def tearDown(self):
        reminders.STATE_FILE = self._orig
        if os.path.exists(self._tmp.name):
            os.unlink(self._tmp.name)

    def _add(self, user_id="100", name="山田"):
        entry, err = reminders.add_reminder(
            channel_id=1, user_id=user_id, user_name=name, content="大会",
            due=reminders.now_jst() + timedelta(days=3), repeat="once")
        self.assertIsNone(err)
        return entry

    def test_admin_can_cancel_others(self):
        e = self._add()
        entry, reason = reminders.cancel_reminder(e["id"], "999",
                                                  is_admin=True)
        self.assertIsNotNone(entry)
        self.assertIsNone(reason)

    def test_not_found_reason(self):
        self.assertEqual(reminders.cancel_reminder(12345, "100"),
                         (None, "not_found"))

    def test_find_entry_for_diagnosis(self):
        e = self._add()
        reminders.cancel_reminder(e["id"], "100")
        old = reminders.find_entry(e["id"])
        self.assertEqual(old["status"], "cancelled")
        self.assertIsNone(reminders.find_entry(99999))

    def test_admin_note_lists_others(self):
        self._add(user_id="100", name="山田")
        mine = reminders.list_active("999")
        note = reminders.build_skill_note(
            reminders.now_jst(), mine, all_entries=reminders.list_active())
        self.assertIn("管理者なので", note)
        self.assertIn("山田さんの分", note)

    def test_non_admin_note_has_no_admin_block(self):
        self._add()
        note = reminders.build_skill_note(reminders.now_jst(),
                                          reminders.list_active("100"))
        self.assertNotIn("管理者なので", note)
