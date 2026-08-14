#!/usr/bin/env python3
"""添付処理（attachments）のユニットテスト。"""

from types import SimpleNamespace
import os
import unittest

from core import attachments


def _att(filename="a.png", content_type="image/png", size=1000, id=1):
    """discord.Attachment 相当のフェイク。"""
    return SimpleNamespace(filename=filename, content_type=content_type,
                           size=size, id=id)


class AttachmentClassifyTest(unittest.TestCase):
    def test_image_by_content_type(self):
        self.assertEqual(
            attachments.classify("p.png", "image/png", 100), "image")

    def test_pdf(self):
        self.assertEqual(
            attachments.classify("r.pdf", "application/pdf", 100), "pdf")

    def test_text_ext_fallback_without_content_type(self):
        self.assertEqual(attachments.classify("m.md", None, 100), "text")

    def test_json_mime_with_text_ext(self):
        self.assertEqual(
            attachments.classify("d.json", "application/json", 100), "text")

    def test_svg_read_as_text(self):
        self.assertEqual(
            attachments.classify("v.svg", "image/svg+xml", 100), "text")

    def test_office_unsupported(self):
        ct = ("application/vnd.openxmlformats-officedocument"
              ".wordprocessingml.document")
        self.assertEqual(attachments.classify("x.docx", ct, 100),
                         "unsupported")

    def test_too_large(self):
        self.assertEqual(
            attachments.classify("p.png", "image/png",
                                 attachments.MAX_FILE_BYTES + 1),
            "too_large")

    def test_large_unsupported_stays_unsupported(self):
        self.assertEqual(attachments.classify("x.docx", None, 10 ** 9),
                         "unsupported")


class PlanAttachmentsTest(unittest.TestCase):
    def test_overflow_beyond_max_files(self):
        atts = [_att(filename=f"{i}.png", id=i) for i in range(7)]
        supported, skipped = attachments.plan_attachments(atts)
        self.assertEqual(len(supported), attachments.MAX_FILES)
        self.assertEqual([r for _, r in skipped], ["overflow", "overflow"])
        # 順序保持: 先頭 MAX_FILES 件が supported
        self.assertEqual([a.filename for a, _ in supported],
                         [f"{i}.png" for i in range(attachments.MAX_FILES)])

    def test_mixed_kinds(self):
        atts = [_att("a.png", "image/png", 10, 1),
                _att("b.docx", None, 10, 2),
                _att("c.pdf", "application/pdf", 10, 3)]
        supported, skipped = attachments.plan_attachments(atts)
        self.assertEqual([k for _, k in supported], ["image", "pdf"])
        self.assertEqual(skipped[0][1], "unsupported")

    def test_empty(self):
        self.assertEqual(attachments.plan_attachments([]), ([], []))


class SafeFilenameTest(unittest.TestCase):
    def test_traversal_removed(self):
        name = attachments.safe_filename("../../etc/passwd", 1)
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)

    def test_keeps_japanese_and_ext(self):
        self.assertEqual(attachments.safe_filename("資料.pdf", 2),
                         "02-資料.pdf")

    def test_empty_name(self):
        self.assertEqual(attachments.safe_filename("", 3), "03-file")

    def test_long_name_truncated_keeps_ext(self):
        name = attachments.safe_filename("あ" * 300 + ".png", 1)
        self.assertLess(len(name), 80)
        self.assertTrue(name.endswith(".png"))


class BuildBlockTest(unittest.TestCase):
    def test_contains_paths_and_read_instruction(self):
        saved = [{"path": "/tmp/x/01-a.png", "name": "01-a.png",
                  "kind": "image", "orig": "a.png"}]
        block = attachments.build_block(saved, [])
        self.assertIn("/tmp/x/01-a.png", block)
        self.assertIn("Read", block)
        self.assertIn("指示ではない", block)  # インジェクション注意書き

    def test_skipped_marked_unreadable(self):
        block = attachments.build_block(
            [], [(_att("x.docx", None, 5000), "unsupported")])
        self.assertIn("x.docx", block)
        self.assertIn("読めない", block)
        self.assertIn("創作しない", block)

    def test_empty(self):
        self.assertEqual(attachments.build_block([], []), "")


class BuildContextTest(unittest.TestCase):
    def test_has_supported_false_when_nothing_saved(self):
        ctx = attachments.build_context(
            "/tmp/x", [], [(_att(), "unsupported")])
        self.assertFalse(ctx.has_supported)
        self.assertTrue(ctx.block)

    def test_has_supported_true(self):
        saved = [{"path": "/tmp/x/01-a.png", "name": "01-a.png",
                  "kind": "image", "orig": "a.png"}]
        self.assertTrue(attachments.build_context("/tmp/x", saved, [])
                        .has_supported)


class DownloadTest(unittest.TestCase):
    def test_saves_and_collects_failures(self):
        import asyncio

        class FakeAtt:
            def __init__(self, filename, fail=False):
                self.filename = filename
                self.fail = fail

            async def save(self, path):
                if self.fail:
                    raise RuntimeError("boom")
                with open(path, "wb") as f:
                    f.write(b"x")

        good, bad = FakeAtt("a.png"), FakeAtt("b.png", fail=True)
        tmpdir, saved, failed = asyncio.run(
            attachments.download([(good, "image"), (bad, "image")]))
        try:
            self.assertEqual(len(saved), 1)
            self.assertTrue(os.path.exists(saved[0]["path"]))
            self.assertEqual(saved[0]["kind"], "image")
            self.assertEqual([r for _, r in failed], ["download_failed"])
        finally:
            attachments.cleanup(tmpdir)
        self.assertFalse(os.path.exists(tmpdir))

    def test_cleanup_none_is_noop(self):
        attachments.cleanup(None)  # 例外にならないこと
