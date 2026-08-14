#!/usr/bin/env python3
"""LLMプロバイダの切り替え（core/llm.py）のテスト。

実際にCLIは起動しない（subprocess を差し替える）。

実行: python -m unittest core.test_llm -v
"""

import subprocess
import unittest
from types import SimpleNamespace

from core import llm

CLAUDE = {"llm": {"provider": "claude"}}
CODEX = {"llm": {"provider": "codex"}}


class _FakeRun:
    """subprocess.run の差し替え。呼ばれた引数を覚える。"""

    def __init__(self, stdout="こたえ", returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        if self.raises:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode,
                               stdout=self.stdout, stderr=self.stderr)


class _Patch:
    """subprocess.run と実行ファイル探索をまとめて差し替える。"""

    def __init__(self, fake, binary="/usr/local/bin/fake"):
        self.fake, self.binary = fake, binary

    def __enter__(self):
        self._run = subprocess.run
        self._find = llm.find_binary
        subprocess.run = self.fake
        llm.find_binary = lambda spec: self.binary
        return self.fake

    def __exit__(self, *exc):
        subprocess.run = self._run
        llm.find_binary = self._find


class ProviderTableTest(unittest.TestCase):
    def test_組み込みが揃っている(self):
        table = llm.provider_table({})
        self.assertIn("claude", table)
        self.assertIn("codex", table)

    def test_利用者定義を足せる(self):
        cfg = {"llm": {"providers": {"mycli": {"command": ["mycli", "-q"]}}}}
        table = llm.provider_table(cfg)
        self.assertEqual(table["mycli"]["command"], ["mycli", "-q"])
        self.assertEqual(table["mycli"]["label"], "mycli")   # 既定のラベル

    def test_利用者定義が組み込みを上書きする(self):
        cfg = {"llm": {"providers": {"claude": {"command": ["custom"]}}}}
        self.assertEqual(
            llm.provider_table(cfg)["claude"]["command"], ["custom"])

    def test_上書きしても他のキーは残る(self):
        cfg = {"llm": {"providers": {"claude": {"default_model": "x"}}}}
        spec = llm.provider_table(cfg)["claude"]
        self.assertEqual(spec["default_model"], "x")
        self.assertTrue(spec["rich"])   # 上書きしていないキーは生きる

    def test_既定はclaude(self):
        self.assertEqual(llm.selected({}), "claude")

    def test_不明なプロバイダは分かるように落ちる(self):
        with self.assertRaises(llm.LLMError) as e:
            llm.spec_for({"llm": {"provider": "nope"}})
        self.assertIn("nope", str(e.exception))
        self.assertIn("claude", str(e.exception))   # 使える名前を示す


class ModelTest(unittest.TestCase):
    def test_設定のモデルが優先(self):
        self.assertEqual(
            llm.model_for({"llm": {"provider": "claude", "model": "m1"}}), "m1")

    def test_未指定ならプロバイダの既定(self):
        self.assertEqual(llm.model_for(CLAUDE), "claude-sonnet-5")
        # codex は既定を持たない（CLI側の既定に任せる）
        self.assertEqual(llm.model_for(CODEX), "")

    def test_タイムアウトは設定できる(self):
        self.assertEqual(llm.timeout_for({"llm": {"timeout_sec": 30}}), 30)
        self.assertEqual(llm.timeout_for({}), llm.DEFAULT_TIMEOUT_SEC)


class BuildArgvTest(unittest.TestCase):
    def test_モデル名が差し込まれる(self):
        with _Patch(_FakeRun()):
            argv = llm.build_argv(llm.BUILTIN_PROVIDERS["claude"], "m9")
        self.assertEqual(argv[0], "/usr/local/bin/fake")
        self.assertIn("m9", argv)

    def test_claudeはツールを無効化して呼ぶ(self):
        # -p はツール実行を挟むと本文が中間メッセージへ消える（既知の落とし穴）
        with _Patch(_FakeRun()):
            argv = llm.build_argv(llm.BUILTIN_PROVIDERS["claude"], "m")
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_未インストールなら導入先を案内する(self):
        saved = llm.find_binary
        llm.find_binary = lambda spec: None
        try:
            with self.assertRaises(llm.LLMError) as e:
                llm.build_argv(llm.BUILTIN_PROVIDERS["codex"], "m")
            self.assertIn("codex", str(e.exception))
            self.assertIn("インストール", str(e.exception))
        finally:
            llm.find_binary = saved

    def test_モデル未指定ならフラグごと落とす(self):
        # 空文字を渡すとCLI側が「不正なモデル名」で落ちる。
        # 契約の種類で使えるモデルが違うツールでは既定に任せる方が確実
        with _Patch(_FakeRun()):
            argv = llm.build_argv(llm.BUILTIN_PROVIDERS["codex"], "")
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[1:], ["exec", "-"])

    def test_モデル指定があればフラグは残る(self):
        with _Patch(_FakeRun()):
            argv = llm.build_argv(llm.BUILTIN_PROVIDERS["codex"], "gpt-5")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5")

    def test_短いフラグ形式も落とせる(self):
        spec = {"command": ["x", "-m", "{model}", "--go"], "bin": "x"}
        with _Patch(_FakeRun()):
            self.assertEqual(llm.build_argv(spec, "")[1:], ["--go"])

    def test_commandが無い定義は落とす(self):
        with self.assertRaises(llm.LLMError):
            llm.build_argv({"label": "壊れた定義"}, "m")


class GenerateTest(unittest.TestCase):
    def test_本文を返す(self):
        with _Patch(_FakeRun(stdout="  こんにちは  ")):
            self.assertEqual(llm.generate("q", CLAUDE), "こんにちは")

    def test_プロンプトは標準入力で渡す(self):
        # 引数で渡すと長文でコマンドライン長の上限に当たる
        with _Patch(_FakeRun()) as fake:
            llm.generate("長い質問", CLAUDE)
        self.assertEqual(fake.calls[0]["input"], "長い質問")

    def test_プロバイダを切り替えるとコマンドが変わる(self):
        with _Patch(_FakeRun()) as fake:
            llm.generate("q", CODEX)
        argv = fake.calls[0]["argv"]
        self.assertIn("exec", argv)      # codex exec ...
        self.assertNotIn("--tools", argv)   # claude 固有の引数が漏れない

    def test_異常終了はstderrを添えて落とす(self):
        with _Patch(_FakeRun(returncode=2, stderr="鍵が無効です")):
            with self.assertRaises(llm.LLMError) as e:
                llm.generate("q", CLAUDE)
        self.assertIn("鍵が無効です", str(e.exception))

    def test_出力が空なら落とす(self):
        # 空文字を回答として通すと、無言の投稿になって原因が分からなくなる
        with _Patch(_FakeRun(stdout="   ")):
            with self.assertRaises(llm.LLMError):
                llm.generate("q", CLAUDE)

    def test_タイムアウトは人間に分かる文にする(self):
        boom = subprocess.TimeoutExpired(cmd="x", timeout=180)
        with _Patch(_FakeRun(raises=boom)):
            with self.assertRaises(llm.LLMError) as e:
                llm.generate("q", CLAUDE)
        self.assertIn("180", str(e.exception))

    def test_起動できない場合も分かるように落とす(self):
        with _Patch(_FakeRun(raises=OSError("Permission denied"))):
            with self.assertRaises(llm.LLMError) as e:
                llm.generate("q", CLAUDE)
        self.assertIn("起動できません", str(e.exception))


class CapabilityTest(unittest.TestCase):
    """できないことを黙って無効化せず、理由を出せること。"""

    def test_claudeは全機能(self):
        self.assertTrue(llm.supports_tools(CLAUDE))
        self.assertEqual(llm.missing_features(CLAUDE), [])
        self.assertEqual(llm.describe_limits(CLAUDE), "")

    def test_他プロバイダは使えない機能を挙げる(self):
        missing = llm.missing_features(CODEX)
        self.assertIn("添付ファイルの読解", missing)
        text = llm.describe_limits(CODEX)
        self.assertIn("Codex", text)
        self.assertIn("添付ファイルの読解", text)

    def test_利用者定義は既定でツール非対応(self):
        cfg = {"llm": {"provider": "mycli",
                       "providers": {"mycli": {"command": ["mycli"]}}}}
        self.assertFalse(llm.supports_tools(cfg))
        self.assertNotEqual(llm.describe_limits(cfg), "")


class DetectTest(unittest.TestCase):
    def test_インストール状況を返す(self):
        saved = llm.find_binary
        llm.find_binary = lambda spec: ("/bin/claude"
                                        if spec.get("bin") == "claude" else None)
        try:
            got = {p["name"]: p for p in llm.detect_available({})}
        finally:
            llm.find_binary = saved
        self.assertTrue(got["claude"]["installed"])
        self.assertEqual(got["claude"]["path"], "/bin/claude")
        self.assertFalse(got["codex"]["installed"])
        self.assertTrue(got["codex"]["install"])   # 導入先を案内できる

    def test_利用者定義も一覧に出る(self):
        cfg = {"llm": {"providers": {"mycli": {"command": ["mycli"]}}}}
        names = [p["name"] for p in llm.detect_available(cfg)]
        self.assertIn("mycli", names)


if __name__ == "__main__":
    unittest.main()
