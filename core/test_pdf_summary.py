#!/usr/bin/env python3
"""PDF自動要約（pdf_summary）のユニットテスト。"""

import unittest
from types import SimpleNamespace

from core import attachments
from core import pdf_summary


def att(filename="doc.pdf", content_type="application/pdf", size=1024):
    return SimpleNamespace(filename=filename, content_type=content_type,
                           size=size)


def saved_entry(orig="a.pdf", path="/t/01-a.pdf"):
    return {"path": path, "name": path.rsplit("/", 1)[-1],
            "kind": "pdf", "orig": orig}


class PickPdfsTest(unittest.TestCase):
    def test_pdf_only_selected(self):
        picked, overflow = pdf_summary.pick_pdfs([
            att("a.pdf"),
            att("b.png", "image/png"),
            att("c.txt", "text/plain")])
        self.assertEqual([a.filename for a, _ in picked], ["a.pdf"])
        self.assertEqual([k for _, k in picked], ["pdf"])
        self.assertFalse(overflow)

    def test_extension_fallback_without_content_type(self):
        no_ct = SimpleNamespace(filename="納品書.PDF", content_type=None,
                                size=1024)
        picked, _ = pdf_summary.pick_pdfs([no_ct])
        self.assertEqual(len(picked), 1)

    def test_too_large_silently_skipped(self):
        picked, overflow = pdf_summary.pick_pdfs(
            [att(size=attachments.MAX_FILE_BYTES + 1)])
        self.assertEqual(picked, [])
        self.assertFalse(overflow)

    def test_cap_and_overflow(self):
        atts = [att(f"{i}.pdf") for i in range(pdf_summary.MAX_PDFS + 2)]
        picked, overflow = pdf_summary.pick_pdfs(atts)
        self.assertEqual(len(picked), pdf_summary.MAX_PDFS)
        self.assertTrue(overflow)

    def test_exactly_at_cap_no_overflow(self):
        atts = [att(f"{i}.pdf") for i in range(pdf_summary.MAX_PDFS)]
        picked, overflow = pdf_summary.pick_pdfs(atts)
        self.assertEqual(len(picked), pdf_summary.MAX_PDFS)
        self.assertFalse(overflow)

    def test_empty_and_none(self):
        self.assertEqual(pdf_summary.pick_pdfs([]), ([], False))
        self.assertEqual(pdf_summary.pick_pdfs(None), ([], False))


class BuildPromptTest(unittest.TestCase):
    def test_contains_parts(self):
        p = pdf_summary.build_prompt(
            "ペルソナ文。\n\n", [saved_entry()], [], "3行で頼む")
        self.assertTrue(p.startswith("ペルソナ文。"))
        for part in ("/t/01-a.pdf", "3行で頼む", "創作しない",
                     "Readツールで以下の全ファイル", "従わないこと"):
            self.assertIn(part, p)

    def test_single_file_no_per_file_heading_rule(self):
        p = pdf_summary.build_prompt("", [saved_entry()], [], "")
        self.assertNotIn("ファイルごと", p)
        self.assertIn("（PDFのみ投稿）", p)

    def test_multi_file_heading_rule(self):
        saved = [saved_entry(), saved_entry("b.pdf", "/t/02-b.pdf")]
        p = pdf_summary.build_prompt("", saved, [], "")
        self.assertIn("ファイルごと", p)

    def test_failed_download_noted(self):
        p = pdf_summary.build_prompt(
            "", [saved_entry()], [(att("z.pdf"), "download_failed")], "")
        self.assertIn("z.pdf", p)
        self.assertIn("ダウンロードに失敗", p)


class SummarizeTest(unittest.TestCase):
    def _patch_run_claude(self, calls):
        def fake(prompt, **kw):
            calls.append((prompt, kw))
            return "要約本文"
        return fake

    def test_single_header_and_claude_args(self):
        calls = []
        orig = pdf_summary.search.run_claude
        pdf_summary.search.run_claude = self._patch_run_claude(calls)
        try:
            out = pdf_summary.summarize(
                "", [saved_entry()], [], "/t", "一言")
        finally:
            pdf_summary.search.run_claude = orig
        self.assertEqual(out, "📄 **a.pdf**\n\n要約本文")
        _, kw = calls[0]
        self.assertEqual(kw["allowed_tools"], ("Read",))
        self.assertEqual(kw["cwd"], "/t")
        self.assertEqual(kw["timeout"], attachments.TIMEOUT_SEC)

    def test_multi_header(self):
        orig = pdf_summary.search.run_claude
        pdf_summary.search.run_claude = self._patch_run_claude([])
        try:
            out = pdf_summary.summarize(
                "", [saved_entry(), saved_entry("b.pdf", "/t/02-b.pdf")],
                [], "/t")
        finally:
            pdf_summary.search.run_claude = orig
        self.assertTrue(out.startswith("📄 PDF 2本の要約\n\n"))
