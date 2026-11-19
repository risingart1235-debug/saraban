"""Registering many documents in a row must not pile up memory.

A finished job (registered or skipped) stays in memory for SARABAN_REDO_DAYS so ♻️ can reopen it,
but it used to keep every layout image as a data-uri (~4 MB a scanned document).  Fifteen documents
in a row took the server from ~100 to ~440 MB - over Render's free 512 MB, so it was killed and
restarted mid-work.  Reopening rebuilds everything from the original, so the layout is dropped.
"""
from __future__ import annotations

import unittest

from tests.test_redo import RedoBase, docmode, pymupdf, settle


@unittest.skipIf(docmode is None or pymupdf is None, "server packages not installed")
class ReleaseLayoutTests(RedoBase):
    def test_a_registered_job_keeps_no_layout_images(self):
        job = self.registered()
        self.assertEqual(job["status"], "done")
        for k in docmode._LAYOUT_KEYS:
            self.assertNotIn(k, job)
        self.assertTrue(job["result"]["ok"])

    def test_a_skipped_job_keeps_no_layout_images(self):
        job = self.from_phone("90")
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready")
        self.assertIn("pages", job)
        docmode.skip(job)
        self.assertNotIn("pages", job)

    def test_redo_still_works_after_the_layout_was_dropped(self):
        job = self.registered()
        again = docmode.reopen_phone(job["book_id"], redo_no=job["receipt_no"])
        self.assertIsNotNone(again)
        result = self.open_and_save(again)
        self.assertEqual(result["receipt_no"], job["receipt_no"])


if __name__ == "__main__":
    unittest.main()
