"""Rendering a PDF page must not leave decoded images behind in PyMuPDF's cache.

PyMuPDF keeps decoded page images in a global store even after the document is closed,
and a document opened again cannot reuse them.  Rendering a 20-page document left about
200 MB behind - the desktop preview now renders every page, and Render's free web
instance has 512 MB in total.  Offline: tiny PDFs made on the spot.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import core

try:
    import pymupdf
except ImportError:          # phone-only setup
    pymupdf = None


@unittest.skipIf(pymupdf is None, "ต้องลง pymupdf ก่อน (pip install -r requirements.txt)")
class RenderCacheTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.pdf = os.path.join(d, "doc.pdf")
        pages = [Image.new("RGB", (400, 560), (255, 255 - 60 * i, 255)) for i in range(3)]
        pages[0].save(self.pdf, "PDF", resolution=50.0, save_all=True, append_images=pages[1:])
        self.emptied = []
        real = pymupdf.TOOLS.store_shrink
        p = patch.object(pymupdf.TOOLS, "store_shrink",
                         side_effect=lambda pct: (self.emptied.append(pct), real(pct))[1])
        p.start()
        self.addCleanup(p.stop)

    def test_desktop_renderer_empties_the_cache_after_every_page(self):
        for n in (1, 2, 3):
            self.assertEqual(core.render_pdf_page(self.pdf, page_number=n, dpi=50).size, (400, 560))
        self.assertEqual(self.emptied, [100, 100, 100])

    def test_even_when_the_page_does_not_exist(self):
        with self.assertRaises(IndexError):
            core.render_pdf_page(self.pdf, page_number=9, dpi=50)
        self.assertEqual(self.emptied, [100])
        self.assertFalse(core.PDF_LOCK.locked())

    def test_web_preview_renderer_too(self):
        from web import docmode
        img, total = docmode._render_pdf_page(self.pdf, 2, 50)
        self.assertEqual((img.size, total), ((400, 560), 3))
        self.assertEqual(self.emptied, [100])


if __name__ == "__main__":
    unittest.main()
