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


if __name__ == "__main__":
    unittest.main()
