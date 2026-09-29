#!/usr/bin/env python3
"""deploy の純粋関数テスト（git/launchctlは起動しない）。

実行: ../chatbot/venv/bin/python -m unittest test_deploy -v
"""

import unittest

from platforms.discord.dev import deploy


class ParsePorcelainTest(unittest.TestCase):
    def test_parses_modified_untracked_and_rename(self):
        # -z 形式: NUL区切り・rename は「新パス\0旧パス」の2レコード
        out = (" M scripts/a.py\0"
               "?? scripts/new.py\0"
               "R  scripts/renamed.py\0scripts/old.py\0")
        self.assertEqual(deploy.parse_porcelain(out),
                         ["scripts/a.py", "scripts/new.py",
                          "scripts/renamed.py"])

    def test_nonascii_paths_survive(self):
        out = "?? scripts/日本語 ファイル.py\0"
        self.assertEqual(deploy.parse_porcelain(out),
                         ["scripts/日本語 ファイル.py"])

    def test_empty_output_means_clean(self):
        self.assertEqual(deploy.parse_porcelain(""), [])
        self.assertEqual(deploy.parse_porcelain(None), [])


class MergeBlockersTest(unittest.TestCase):
    def test_overlap_is_detected_sorted(self):
        got = deploy.merge_blockers(
            ["s/b.py", "s/a.py", "s/only_dirty.py"],
            ["s/a.py", "s/b.py", "s/only_incoming.py"])
        self.assertEqual(got, ["s/a.py", "s/b.py"])

    def test_disjoint_changes_do_not_block(self):
        self.assertEqual(deploy.merge_blockers(["x.py"], ["y.py"]), [])
        self.assertEqual(deploy.merge_blockers([], ["y.py"]), [])



class ProtectedChangesTest(unittest.TestCase):
    """反映直前の最終防衛線。書き込み手段（Write/Edit/Bash）に関係なく、
    開発BOT自身のコードに触れる差分は反映させない。"""

    def test_own_code_is_caught(self):
        got = deploy.protected_changes([
            "platforms/discord/dev/bot.py",
            "platforms/discord/dev/dev_pipeline.py",
            "platforms/discord/bot.py",
        ])
        self.assertEqual(got, ["platforms/discord/dev/bot.py",
                               "platforms/discord/dev/dev_pipeline.py"])

    def test_new_file_in_own_dir_is_caught(self):
        self.assertEqual(
            deploy.protected_changes(["platforms/discord/dev/new_helper.py"]),
            ["platforms/discord/dev/new_helper.py"])

    def test_windows_separators_are_caught(self):
        self.assertEqual(
            deploy.protected_changes(["platforms\\discord\\dev\\bot.py"]),
            ["platforms\\discord\\dev\\bot.py"])

    def test_ordinary_changes_pass(self):
        self.assertEqual(deploy.protected_changes([
            "core/reminders.py", "dashboard/web/App.tsx"]), [])

    def test_lookalike_dirs_pass(self):
        # "dev" を含むだけの別ディレクトリは対象外
        self.assertEqual(deploy.protected_changes(
            ["platforms/discord/dev-notes/x.md"]), [])

if __name__ == "__main__":
    unittest.main()
