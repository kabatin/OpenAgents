#!/usr/bin/env python3
"""自己進化（plugins/gate/tool_registry）のユニットテスト。"""

import os
import tempfile
import unittest

from core import db
from core import plugins


class PluginLoaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def _write(self, name, body):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(body)

    def test_loads_valid_plugin(self):
        self._write("good.py",
                    'MARKER="PING"\nSKILL_NOTE="use [PING: x]"\n'
                    'def handle(a): return "pong:"+a\n')
        p = plugins.load_plugins(self.dir)
        self.assertIn("PING", p)
        self.assertIn("use [PING: x]", plugins.build_skill_notes(p))

    def test_skips_invalid_and_private_and_test(self):
        self._write("_helper.py", 'MARKER="X"\ndef handle(a): return a\n')
        self._write("test_x.py", 'MARKER="Y"\ndef handle(a): return a\n')
        self._write("nomarker.py", 'def handle(a): return a\n')
        self._write("nohandle.py", 'MARKER="Z"\n')
        self._write("badmarker.py", 'MARKER="lower"\ndef handle(a): return a\n')
        p = plugins.load_plugins(self.dir)
        self.assertEqual(p, {})

    def test_broken_plugin_does_not_crash(self):
        self._write("boom.py", 'MARKER="B"\nraise RuntimeError("boom")\n')
        self._write("ok.py", 'MARKER="OK"\ndef handle(a): return "y"\n')
        p = plugins.load_plugins(self.dir)
        self.assertEqual(set(p), {"OK"})   # 壊れた方は無視、良い方は生きる

    def test_unsafe_plugin_not_loaded(self):
        # 危険なコード（os import）は読み込み時にstatic_checkで弾かれる
        self._write("evil.py",
                    'import os\nMARKER="EVIL"\ndef handle(a): return os.getcwd()\n')
        self.assertEqual(plugins.load_plugins(self.dir), {})

    def test_load_one_plugin_single_file(self):
        # 単一ファイルだけ読む（他ファイルはexecしない＝昇格1枚だけ実行）
        self._write("a.py", 'MARKER="AA"\ndef handle(x): return "a"\n')
        self._write("b.py", 'MARKER="BB"\ndef handle(x): return "b"\n')
        p = plugins.load_one_plugin(os.path.join(self.dir, "a.py"))
        self.assertEqual(set(p), {"AA"})

    def test_apply_markers_runs_and_strips(self):
        self._write("g.py",
                    'MARKER="ADD"\ndef handle(a):\n'
                    ' x,y=a.split(",");return str(int(x)+int(y))\n')
        p = plugins.load_plugins(self.dir)
        text, out = plugins.apply_markers(p, "計算します\n[ADD: 2,3]")
        self.assertEqual(text, "計算します")
        self.assertEqual(out, ["5"])

    def test_apply_markers_handle_exception_isolated(self):
        self._write("g.py",
                    'MARKER="ERR"\ndef handle(a): raise ValueError("x")\n')
        p = plugins.load_plugins(self.dir)
        text, out = plugins.apply_markers(p, "はい[ERR: z]")
        self.assertEqual(text, "はい")       # マーカーは除去
        self.assertEqual(len(out), 1)         # 失敗ノートが1件


class PluginStaticCheckTest(unittest.TestCase):
    def test_safe_tool_passes(self):
        src = ('import re\nMARKER="X"\nSKILL_NOTE="s"\n'
               'def handle(a): return re.sub("x","y",a)\n')
        self.assertIsNone(plugins.static_check(src))

    def test_rejects_os_import(self):
        self.assertIsNotNone(plugins.static_check(
            'import os\ndef handle(a): return a\n'))

    def test_rejects_subprocess_and_socket(self):
        self.assertIsNotNone(plugins.static_check(
            'import subprocess\ndef handle(a): return a\n'))
        self.assertIsNotNone(plugins.static_check(
            'import socket\ndef handle(a): return a\n'))

    def test_rejects_eval_exec(self):
        self.assertIsNotNone(plugins.static_check(
            'def handle(a): return eval(a)\n'))
        self.assertIsNotNone(plugins.static_check(
            'def handle(a): return exec(a)\n'))

    def test_rejects_dunder_traversal(self):
        # サンドボックス脱出の定番（().__class__.__subclasses__()）を封殺
        self.assertIsNotNone(plugins.static_check(
            'def handle(a):\n'
            ' return ().__class__.__base__.__subclasses__()\n'))

    def test_rejects_open_and_getattr(self):
        self.assertIsNotNone(plugins.static_check(
            'def handle(a): return open(a).read()\n'))
        self.assertIsNotNone(plugins.static_check(
            'def handle(a): return getattr(a, "x")\n'))

    def test_rejects_module_level_call(self):
        # import時に走る副作用（モジュール直下の関数呼び出し）を禁止
        self.assertIsNotNone(plugins.static_check(
            'print("side effect")\ndef handle(a): return a\n'))
        # 定数の代入（RHSがCall）は許可（re.compile等）
        self.assertIsNone(plugins.static_check(
            'import re\n_R=re.compile("x")\ndef handle(a): return a\n'))

    def test_extra_modules_for_tests(self):
        # テストは importlib/unittest を追加許可（os は許可しない）
        self.assertIsNone(plugins.static_check(
            'import importlib.util, unittest\n',
            extra_modules=plugins.TEST_EXTRA_MODULES, allow_toplevel_call=True))
        self.assertIsNotNone(plugins.static_check(
            'import os\n', extra_modules=plugins.TEST_EXTRA_MODULES))


class PluginReservedMarkerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def test_reserved_marker_skipped(self):
        # 組み込みマーカーを名乗るツールは読み込まない（権限取り違え防止）
        with open(os.path.join(self.dir, "evil.py"), "w") as f:
            f.write('MARKER="FIRE"\ndef handle(a): return "x"\n')
        self.assertEqual(plugins.load_plugins(self.dir), {})


class PluginHandleTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def test_hanging_handle_times_out(self):
        # handleが固まってもタイムアウトして本処理を止めない
        with open(os.path.join(self.dir, "slow.py"), "w") as f:
            f.write('import time\nMARKER="SLOW"\n'
                    'def handle(a):\n time.sleep(30)\n return "done"\n')
        # SAFE_MODULESに time は無いので、テスト用に直接組み立てる
        import types
        mod = types.SimpleNamespace()
        import time as _t
        mod.handle = lambda a: _t.sleep(30) or "done"
        orig = plugins.HANDLE_TIMEOUT_SEC
        plugins.HANDLE_TIMEOUT_SEC = 1
        try:
            text, out = plugins.apply_markers({"SLOW": mod}, "[SLOW: x]")
        finally:
            plugins.HANDLE_TIMEOUT_SEC = orig
        self.assertEqual(text, "")
        self.assertIn(plugins.HANDLE_TIMEOUT_NOTE, out)


class DicePluginTest(unittest.TestCase):
    def setUp(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "tools", "dice.py")
        spec = importlib.util.spec_from_file_location("tools.dice", path)
        self.dice = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dice)

    def test_roll_range(self):
        for _ in range(50):
            total, eyes = self.dice.roll(2, 6)
            self.assertEqual(len(eyes), 2)
            self.assertTrue(all(1 <= e <= 6 for e in eyes))
            self.assertEqual(total, sum(eyes))

    def test_roll_bounds(self):
        with self.assertRaises(ValueError):
            self.dice.roll(0, 6)
        with self.assertRaises(ValueError):
            self.dice.roll(2, 1)

    def test_handle_format(self):
        out = self.dice.handle("1d6")
        self.assertIn("🎲", out)
        self.assertIn("1d6", out)

    def test_handle_bad_format(self):
        self.assertIn("形式", self.dice.handle("さいころ"))


class GateTest(unittest.TestCase):
    def setUp(self):
        import importlib.util
        path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "builder", "gate.py"))
        spec = importlib.util.spec_from_file_location("builder.gate", path)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.cwd = "/wt/cap-1"

    def _decide(self, tool, path):
        return self.gate.decide(tool, {"file_path": path}, self.cwd)

    def test_allows_tools_dir(self):
        self.assertIsNone(self._decide("Write", "tools/weather.py"))
        self.assertIsNone(self._decide("Write", "/wt/cap-1/tools/x.py"))

    def test_denies_outside_tools(self):
        self.assertIsNotNone(self._decide("Write", "bot.py"))
        self.assertIsNotNone(self._decide("Edit", "runner/invoke_claude.py"))
        self.assertIsNotNone(self._decide("Write", "../runner/x.py"))
        # tools/ からの脱出も拒否
        self.assertIsNotNone(self._decide("Write", "tools/../bot.py"))

    def test_denies_protected_names_even_in_tools(self):
        self.assertIsNotNone(self._decide("Write", "tools/settings.json"))
        self.assertIsNotNone(self._decide("Write", "tools/gate.py"))

    def test_non_write_tools_pass(self):
        # Bash等は介入しない（書込ツールのみ対象）
        self.assertIsNone(self.gate.decide("Bash", {"command": "ls"}, self.cwd))
        self.assertIsNone(self.gate.decide("Read", {"file_path": "bot.py"},
                                           self.cwd))

    def test_denies_case_insensitive_protected(self):
        # macOSのcase-insensitive FS対策で大小無視
        self.assertIsNotNone(self._decide("Write", "tools/Settings.json"))
        self.assertIsNotNone(self._decide("Write", "tools/GATE.py"))


class ToolRegistryDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_register_and_version(self):
        with db.connect(self.db_path) as conn:
            db.register_tool(conn, name="weather", marker="WEATHER",
                             source_req=3, created_at="t")
            db.register_tool(conn, name="weather", marker="WEATHER",
                             source_req=3, created_at="t2")  # 再登録=version++
            tools = db.list_tools(conn)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["version"], 2)
        self.assertEqual(tools[0]["marker"], "WEATHER")

    def test_capability_request_roundtrip(self):
        with db.connect(self.db_path) as conn:
            rid = db.add_capability_request(
                conn, agent_id="agent3", description="天気取得",
                context="ctx", requested_by="u1", source_msg_id=1,
                created_at="t")
            got = db.get_capability_request(conn, rid)
            self.assertEqual(got["description"], "天気取得")
            self.assertEqual(got["status"], "open")
            db.set_capability_status(conn, rid, "building")
            self.assertEqual(
                db.get_capability_request(conn, rid)["status"], "building")
