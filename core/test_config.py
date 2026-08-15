#!/usr/bin/env python3
"""設定の読み込みと検査（core/config.py）のテスト。

実行: python -m unittest core.test_config -v
"""

import json
import os
import tempfile
import unittest

from core import config


def _cfg(**overrides):
    base = {
        "guild_id": "1234567890123456789",
        "agents": [{
            "id": "agent1", "name": "エージェント", "token": "t",
            "home_channel_id": "1234567890123456780", "archiver": True,
            "persona_files": [],
        }],
    }
    base.update(overrides)
    return base


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_設定ファイルが無くても落ちない(self):
        # 初回起動。ここで例外にすると、設定を作る画面すら開けなくなる
        got = config.load(os.path.join(self.tmp, "no-such.json"))
        self.assertEqual(got["agents"], [])

    def test_既定値は毎回別オブジェクト(self):
        a = config.load(os.path.join(self.tmp, "none.json"))
        a["agents"].append({"id": "x"})
        b = config.load(os.path.join(self.tmp, "none.json"))
        self.assertEqual(b["agents"], [])

    def test_壊れたJSONは既定値に倒さず落とす(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ これは JSON ではない")
        with self.assertRaises(config.ConfigError) as e:
            config.load(self.path)
        self.assertIn("壊れて", str(e.exception))

    def test_オブジェクトでない中身は拒否する(self):
        self._write([1, 2, 3])
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_普通に読める(self):
        self._write(_cfg())
        got = config.load(self.path)
        self.assertEqual(got["agents"][0]["id"], "agent1")

    def test_19桁IDは文字列のまま保たれる(self):
        self._write(_cfg())
        got = config.load(self.path)
        self.assertEqual(got["guild_id"], "1234567890123456789")


class EnvExpansionTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        os.environ["OA_TEST_TOKEN"] = "secret-value"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_環境変数参照を展開する(self):
        got = config.expand_env({"token": "${OA_TEST_TOKEN}"})
        self.assertEqual(got["token"], "secret-value")

    def test_入れ子とリストも展開する(self):
        got = config.expand_env(
            {"agents": [{"token": "${OA_TEST_TOKEN}"}]})
        self.assertEqual(got["agents"][0]["token"], "secret-value")

    def test_未定義の変数は空文字に潰さず落とす(self):
        # 空文字にすると「トークン未設定」として静かに起動しないBOTになる
        with self.assertRaises(config.ConfigError) as e:
            config.expand_env({"token": "${OA_UNDEFINED_VAR}"})
        self.assertIn("OA_UNDEFINED_VAR", str(e.exception))

    def test_strictでなければ参照文字列を残す(self):
        got = config.expand_env({"token": "${OA_UNDEFINED_VAR}"}, strict=False)
        self.assertEqual(got["token"], "${OA_UNDEFINED_VAR}")

    def test_ただの文字列は触らない(self):
        for value in ("MTIzNDU2", "1234567890123456789", "", "$NOT_A_REF",
                      "前置き ${OA_TEST_TOKEN} 後置き"):
            self.assertEqual(config.expand_env({"v": value})["v"], value)

    def test_文字列以外は触らない(self):
        got = config.expand_env({"n": 10, "b": True, "z": None})
        self.assertEqual(got, {"n": 10, "b": True, "z": None})


class DotenvTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp = tempfile.mkdtemp()
        self.env = os.path.join(self.tmp, ".env")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def _write(self, text):
        with open(self.env, "w", encoding="utf-8") as f:
            f.write(text)

    def test_KEY_VALUE_を読む(self):
        self._write("OA_FROM_DOTENV=hello\n")
        config._load_dotenv(self.env)
        self.assertEqual(os.environ["OA_FROM_DOTENV"], "hello")

    def test_コメントと空行を飛ばす(self):
        self._write("# comment\n\nOA_D2=v2\n")
        config._load_dotenv(self.env)
        self.assertEqual(os.environ["OA_D2"], "v2")

    def test_引用符を外す(self):
        self._write('OA_D3="quoted"\nOA_D4=\'single\'\n')
        config._load_dotenv(self.env)
        self.assertEqual(os.environ["OA_D3"], "quoted")
        self.assertEqual(os.environ["OA_D4"], "single")

    def test_既存の環境変数を上書きしない(self):
        os.environ["OA_D5"] = "from-shell"
        self._write("OA_D5=from-file\n")
        config._load_dotenv(self.env)
        self.assertEqual(os.environ["OA_D5"], "from-shell")

    def test_ファイルが無くても落ちない(self):
        config._load_dotenv(os.path.join(self.tmp, "no-such.env"))


class ValidateTest(unittest.TestCase):
    def test_最小構成は通る(self):
        self.assertEqual(config.validate(_cfg()), [])

    def test_エージェント未登録は起動不可(self):
        problems = config.validate(_cfg(agents=[]))
        self.assertTrue(any("エージェント" in p for p in problems))

    def test_サーバー未設定は起動不可(self):
        problems = config.validate(_cfg(guild_id=""))
        self.assertTrue(any("サーバー" in p for p in problems))

    def test_記録担当は必ず1体(self):
        two = _cfg()["agents"] + [{
            "id": "agent2", "name": "2", "token": "t",
            "home_channel_id": "2", "archiver": True}]
        self.assertTrue(any("ちょうど1体" in p
                            for p in config.validate(_cfg(agents=two))))
        none = [{**_cfg()["agents"][0], "archiver": False}]
        self.assertTrue(any("ちょうど1体" in p
                            for p in config.validate(_cfg(agents=none))))

    def test_記録担当のトークン未設定は起動不可(self):
        no_token = [{**_cfg()["agents"][0], "token": "  "}]
        problems = config.validate(_cfg(agents=no_token))
        self.assertTrue(any("トークン" in p for p in problems))

    def test_IDの重複を弾く(self):
        dup = _cfg()["agents"] + [{
            "id": "agent1", "name": "2", "token": "t",
            "home_channel_id": "2"}]
        self.assertTrue(any("重複" in p
                            for p in config.validate(_cfg(agents=dup))))

    def test_必須項目の欠けを個別に報告する(self):
        broken = [{"id": "", "name": "", "archiver": True, "token": "t"}]
        problems = config.validate(_cfg(agents=broken))
        self.assertTrue(any("id" in p for p in problems))
        self.assertTrue(any("表示名" in p for p in problems))
        self.assertTrue(any("ホームチャンネル" in p for p in problems))

    def test_問題は全部まとめて返す(self):
        # 1つ直すたびに次の理由で落ちる、を繰り返させない
        problems = config.validate({"agents": []})
        self.assertGreaterEqual(len(problems), 2)

    def test_古い形式は移行を促す(self):
        problems = config.validate({"discord_token": "t", "guild_id": "1"})
        self.assertEqual(len(problems), 1)
        self.assertIn("古い形式", problems[0])

    def test_開発BOTは有効なときだけトークンを要求する(self):
        off = _cfg(dev_bot={"enabled": False})
        self.assertEqual(config.validate(off), [])
        on = _cfg(dev_bot={"enabled": True, "token": ""})
        self.assertTrue(any("開発BOT" in p for p in config.validate(on)))

    def test_議事録BOTは有効なときだけ設定を要求する(self):
        off = _cfg(meeting_bot={"enabled": False})
        self.assertEqual(config.validate(off), [])
        on = _cfg(meeting_bot={"enabled": True, "token": "t"})
        self.assertTrue(any("ボイスチャンネル" in p for p in config.validate(on)))

    def test_require_validは理由を並べて投げる(self):
        with self.assertRaises(config.ConfigError) as e:
            config.require_valid({"agents": []})
        text = str(e.exception)
        self.assertIn("起動できません", text)
        self.assertIn("エージェント", text)

    def test_require_validは通れば何もしない(self):
        config.require_valid(_cfg())


class ExampleConfigTest(unittest.TestCase):
    """同梱の config.example.json が、そのまま使える形であること。"""

    def test_例はトークン以外の不備がない(self):
        from core import paths
        cfg = config.load(paths.CONFIG_EXAMPLE_PATH, expand=False)
        problems = [p for p in config.validate(cfg) if "トークン" not in p]
        self.assertEqual(problems, [])

    def test_例のペルソナファイルは実在する(self):
        from core import paths
        cfg = config.load(paths.CONFIG_EXAMPLE_PATH, expand=False)
        for agent in cfg["agents"]:
            for rel in agent.get("persona_files") or []:
                self.assertTrue(os.path.exists(paths.resolve(rel)),
                                f"{rel} が見つかりません")


class GuildMismatchTest(unittest.TestCase):
    """繋ぎ先だけ別サーバーに変えられていないか。

    会話の記録は guild_id を持たないので、混ざると選んで消せない。
    エージェントが他所のサーバーの会話を引用する事故になる。
    """

    def test_same_guild_is_fine(self):
        self.assertIsNone(config.guild_mismatch_problem("100", "100"))

    def test_type_difference_is_not_a_mismatch(self):
        # config は文字列、GUILD_ID は int で渡ってくる
        self.assertIsNone(config.guild_mismatch_problem("100", 100))

    def test_unrecorded_archive_is_fine(self):
        # 初回起動と、meta が無い時代の既存DB。次の記録時に覚える
        for stored in (None, ""):
            self.assertIsNone(config.guild_mismatch_problem(stored, "100"))

    def test_unset_config_is_left_to_validate(self):
        for current in (None, "", 0):
            self.assertIsNone(config.guild_mismatch_problem("100", current))

    def test_different_guild_is_reported_with_both_ids(self):
        msg = config.guild_mismatch_problem("100", "200")
        self.assertIsNotNone(msg)
        self.assertIn("100", msg)
        self.assertIn("200", msg)
        self.assertIn("初期化", msg)


class AgentByIdTest(unittest.TestCase):
    def test_引ける(self):
        self.assertEqual(config.agent_by_id(_cfg(), "agent1")["name"],
                         "エージェント")

    def test_無ければNone(self):
        self.assertIsNone(config.agent_by_id(_cfg(), "nope"))
        self.assertIsNone(config.agent_by_id({}, "agent1"))


if __name__ == "__main__":
    unittest.main()
